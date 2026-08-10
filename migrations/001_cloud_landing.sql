-- Conjure cloud-landing: per-user models + device pairing (InsForge / Postgres)
-- Apply via InsForge SQL console or CLI when the project backend is available.

CREATE TABLE IF NOT EXISTS user_models (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  prompt        text NOT NULL DEFAULT '',
  source        text NOT NULL DEFAULT 'meshy',
  glb_url       text,
  stl_url       text,
  glb_key       text,
  stl_key       text,
  local_model_id text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_models_user_id_idx ON user_models (user_id);
CREATE INDEX IF NOT EXISTS user_models_created_at_idx ON user_models (created_at DESC);

ALTER TABLE user_models ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_models_own ON user_models;
CREATE POLICY user_models_own ON user_models
  FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE TABLE IF NOT EXISTS devices (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  name               text NOT NULL DEFAULT 'Conjure Kiosk',
  device_secret_hash text NOT NULL,
  pairing_code       text,
  pairing_expires_at timestamptz,
  last_seen_at       timestamptz,
  base_url           text,
  status             text NOT NULL DEFAULT 'offline',
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS devices_user_id_idx ON devices (user_id);
CREATE INDEX IF NOT EXISTS devices_pairing_code_idx ON devices (pairing_code);

ALTER TABLE devices ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS devices_own_select ON devices;
CREATE POLICY devices_own_select ON devices
  FOR SELECT
  USING (auth.uid() = user_id);

DROP POLICY IF EXISTS devices_own_update ON devices;
CREATE POLICY devices_own_update ON devices
  FOR UPDATE
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Inbox: studio pushes a cloud model onto a paired device
CREATE TABLE IF NOT EXISTS device_inbox (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id    uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  model_id     uuid NOT NULL REFERENCES user_models(id) ON DELETE CASCADE,
  status       text NOT NULL DEFAULT 'pending',
  created_at   timestamptz NOT NULL DEFAULT now(),
  claimed_at   timestamptz
);

CREATE INDEX IF NOT EXISTS device_inbox_device_pending_idx
  ON device_inbox (device_id) WHERE status = 'pending';

ALTER TABLE device_inbox ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS device_inbox_via_device ON device_inbox;
CREATE POLICY device_inbox_via_device ON device_inbox
  FOR ALL
  USING (
    EXISTS (
      SELECT 1 FROM devices d
      WHERE d.id = device_inbox.device_id AND d.user_id = auth.uid()
    )
  )
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM devices d
      WHERE d.id = device_inbox.device_id AND d.user_id = auth.uid()
    )
  );
