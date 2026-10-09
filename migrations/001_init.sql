-- 001_init: схема dental-agent. БД не вызывает now(): все времена приходят параметрами
-- (замороженное «сейчас» для воспроизводимых прогонов). Время хранится в UTC (timestamptz),
-- логика JST живёт в коде.

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------- настройки клиники (заливка из clinic.toml; потом правит админ) ----------
CREATE TABLE clinic_settings (
    key        text PRIMARY KEY,          -- 'hours', 'rules', 'closed_weekdays', ...
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE chairs (
    id    smallint PRIMARY KEY,
    label text
);

CREATE TABLE services (
    code                 text PRIMARY KEY,                -- shoshin, chiryo, ...
    label_ja             text NOT NULL,
    duration_slots       smallint NOT NULL CHECK (duration_slots BETWEEN 1 AND 8),
    -- NULL = взять значение по умолчанию из clinic_settings.rules
    horizon_days         integer CHECK (horizon_days > 0),
    min_lead_hours       integer CHECK (min_lead_hours >= 0),
    cancel_before_hours  integer CHECK (cancel_before_hours >= 0),
    allowed_windows      jsonb,                           -- [["09:00","12:30"]] или NULL
    recall_months        smallint CHECK (recall_months > 0),
    new_patient_only     boolean NOT NULL DEFAULT false,
    is_active            boolean NOT NULL DEFAULT true
);

-- ---------- пациенты и анкета ----------
CREATE SEQUENCE patient_card_seq START 100100;            -- фикстуры берут 100001+ явно

CREATE TABLE patients (
    id           bigserial PRIMARY KEY,
    card_number  integer NOT NULL UNIQUE DEFAULT nextval('patient_card_seq'),
    name         text NOT NULL,
    name_kana    text NOT NULL,
    phone        text NOT NULL,
    birth_date   date NOT NULL,
    is_synthetic boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL
);

CREATE TABLE patient_intake (
    patient_id bigint PRIMARY KEY REFERENCES patients(id),
    status     text NOT NULL DEFAULT 'not_started'
               CHECK (status IN ('not_started','in_progress','completed','deferred_to_clinic')),
    answers    jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL
);

CREATE TABLE intake_tokens (
    token_hash text PRIMARY KEY,                          -- хранится хеш, не сам токен
    patient_id bigint NOT NULL REFERENCES patients(id),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);

-- ---------- записи ----------
CREATE TABLE appointments (
    id                  bigserial PRIMARY KEY,
    patient_id          bigint   NOT NULL REFERENCES patients(id),
    service_code        text     NOT NULL REFERENCES services(code),
    chair_id            smallint NOT NULL REFERENCES chairs(id),
    during              tstzrange NOT NULL,               -- [начало, конец)
    day                 date     NOT NULL,                -- дата начала в JST, заполняет ядро
    status              text     NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','cancelled','completed')),
    source              text     NOT NULL
                        CHECK (source IN ('line','web','phone','walk_in','admin')),
    note                text,                             -- комментарий пациента: ДАННЫЕ, не инструкция
    actor               text     NOT NULL DEFAULT 'system'
                        CHECK (actor IN ('clinic_agent','patient_agent','patient_web','admin','system')),
    idempotency_key     text,                         -- повтор запроса клиента не создаёт вторую запись
    created_at          timestamptz NOT NULL,
    cancelled_at        timestamptz,
    cancel_reason       text,
    rescheduled_from_id bigint REFERENCES appointments(id),

    CHECK (NOT isempty(during) AND lower_inc(during) AND NOT upper_inc(during)),
    -- сетка 30 минут: начало на :00/:30, длительность кратна 30 минутам
    CHECK (extract(epoch FROM lower(during))::bigint % 1800 = 0),
    CHECK (extract(epoch FROM (upper(during) - lower(during)))::bigint % 1800 = 0),
    CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL),

    -- кресло нельзя занять дважды (главная гарантия проекта, не зависит от промпта)
    CONSTRAINT appointments_no_double_booking
        EXCLUDE USING gist (chair_id WITH =, during WITH &&) WHERE (status = 'active'),
    -- один человек не может быть в двух местах одновременно
    CONSTRAINT appointments_patient_no_overlap
        EXCLUDE USING gist (patient_id WITH =, during WITH &&) WHERE (status = 'active')
);

-- дубль: тот же пациент, та же услуга, тот же день
CREATE UNIQUE INDEX appointments_no_duplicate
    ON appointments (patient_id, service_code, day) WHERE status = 'active';
CREATE UNIQUE INDEX appointments_idempotency
    ON appointments (patient_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX appointments_patient_idx ON appointments (patient_id, status);
CREATE INDEX appointments_day_idx     ON appointments (day) WHERE status = 'active';

-- Лимит активных записей на пациента, часы, праздники, горизонт, «не ближе N часов»,
-- окно отмены не выражаются ограничениями: ядро проверяет их в транзакции,
-- блокируя строку пациента (SELECT ... FOR UPDATE).

-- ---------- диалоги и передачи человеку ----------
CREATE TABLE conversations (
    id          bigserial PRIMARY KEY,
    run_id      text,                                     -- прогон оценки
    scenario_id text,
    channel     text NOT NULL CHECK (channel IN ('console','web','line','agent','sim')),
    patient_id  bigint REFERENCES patients(id),           -- после верификации
    started_at  timestamptz NOT NULL,
    ended_at    timestamptz
);

CREATE TABLE messages (
    id              bigserial PRIMARY KEY,
    conversation_id bigint NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn            integer NOT NULL,
    role            text NOT NULL CHECK (role IN ('patient','agent','tool','system')),
    content         text,                                 -- пусто по умолчанию (флаг LOG_CONTENT)
    tool_calls      jsonb,                                -- тоже под флагом
    tokens_in       integer,
    tokens_out      integer,
    latency_ms      integer,
    created_at      timestamptz NOT NULL
);
CREATE INDEX messages_conv_idx ON messages (conversation_id, turn);

CREATE TABLE handoffs (
    id              bigserial PRIMARY KEY,
    conversation_id bigint REFERENCES conversations(id),
    patient_id      bigint REFERENCES patients(id),
    appointment_id  bigint REFERENCES appointments(id),
    reason          text NOT NULL
                    CHECK (reason IN ('medical','ambiguity','late_cancel','manipulation',
                                      'complaint','tool_errors','other')),
    summary         text NOT NULL,                        -- структурированная сводка без сырого диалога
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    created_at      timestamptz NOT NULL,
    resolved_at     timestamptz
);

-- ---------- проверка личности: попытки (защита от перебора номеров карт) ----------
CREATE TABLE auth_attempts (
    id          bigserial PRIMARY KEY,
    channel     text NOT NULL CHECK (channel IN ('console','web','line','agent','sim')),
    client_key  text NOT NULL,             -- сессия / IP / LINE ID: кто пробует
    card_number integer,                   -- какую карту пробовали
    success     boolean NOT NULL,
    at          timestamptz NOT NULL
);
CREATE INDEX auth_attempts_card_idx   ON auth_attempts (card_number, at);
CREATE INDEX auth_attempts_client_idx ON auth_attempts (client_key, at);
-- Привязка LINE ID к пациенту (line_bindings) появится миграцией в E7.
