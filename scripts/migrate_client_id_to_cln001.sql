-- =====================================================
-- MIGRATION: Rename client_id deepti_dental → cln001
-- Run on: live server DB + Supabase
-- Safe to re-run (INSERT uses ON CONFLICT DO NOTHING)
-- =====================================================

BEGIN;

-- Step 1: Insert new clients row with client_id = cln001
-- (copies all data from deepti_dental row)
INSERT INTO clients (
    client_id, did_number, clinic_name, doctor_name, doctor_qualifications,
    address, timings, doctor_mobile,
    consultation_fee_min, consultation_fee_max,
    default_language, emergency_transfer_number,
    connection_id, config_version,
    morning_open, morning_close, evening_open, evening_close,
    closed_weekdays, services,
    created_at, updated_at
)
SELECT
    'cln001', did_number, clinic_name, doctor_name, doctor_qualifications,
    address, timings, doctor_mobile,
    consultation_fee_min, consultation_fee_max,
    default_language, emergency_transfer_number,
    connection_id, config_version,
    morning_open, morning_close, evening_open, evening_close,
    closed_weekdays, services,
    created_at, updated_at
FROM clients
WHERE client_id = 'deepti_dental'
ON CONFLICT (client_id) DO NOTHING;

-- Step 2: Update all FK-referencing tables (children first, PK last)
UPDATE app_users           SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE calendar_connections SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE sessions            SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE appointments        SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE agent_appointments  SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE call_logs           SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE llm_events          SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE stt_events          SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE tts_events          SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE audit_log           SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE cancellations       SET client_id = 'cln001' WHERE client_id = 'deepti_dental';
UPDATE reschedules         SET client_id = 'cln001' WHERE client_id = 'deepti_dental';

-- Step 3: Remove the old clients row
DELETE FROM clients WHERE client_id = 'deepti_dental';

COMMIT;
