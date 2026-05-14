-- Target:
-- Server: VPS2_PGSQL
-- Database: csbdb
-- Table: public.stops
-- Purpose: Set all stops to the same lat/lng

DO $$
BEGIN
    IF current_database() <> 'csbdb' THEN
        RAISE EXCEPTION 'Wrong database: %, expected csbdb', current_database();
    END IF;

    IF to_regclass('public.stops') IS NULL THEN
        RAISE EXCEPTION 'Table public.stops does not exist in this database';
    END IF;
END $$;

UPDATE public.stops
SET
    lat = 22.582200,
    lng = 88.485600
RETURNING id, name, lat, lng;

SELECT id, name, lat, lng
FROM public.stops
ORDER BY id ASC;