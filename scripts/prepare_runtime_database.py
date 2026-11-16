"""Provision a restricted runtime role from the deployment host.

Uses the existing database container's local administrative connection. Never
prints a password or URL. Existing runtime credentials are reused on rerun.
Run before deploying the production Compose file with separate migration role.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit


def prepare(directory: Path, container: str) -> None:
    directory = directory.resolve(strict=True)
    env_file = directory / ".env"
    if env_file.is_symlink() or not env_file.is_file():
        raise ValueError("Expected a regular server environment file")
    original = env_file.read_text(encoding="utf8")
    values = {}
    for line in original.splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    existing = values.get('RUNTIME_DATABASE_URL')
    if existing:
        parsed = urlsplit(existing)
        if parsed.username != 'savegeo_runtime' or parsed.hostname != 'db' or parsed.path != '/savegeo':
            raise ValueError('Unexpected existing runtime database identity')
        from urllib.parse import unquote
        password = unquote(parsed.password or '')
    else:
        password = secrets.token_urlsafe(48)
    # PostgreSQL literal escaping; role/database identifiers below are constants.
    literal = "'" + password.replace("'", "''") + "'"
    statement = f"""
    BEGIN;
    DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='savegeo_runtime') THEN CREATE ROLE savegeo_runtime; END IF; END$$;
    ALTER ROLE savegeo_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {literal};
    DO $$BEGIN IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname='savegeo_runtime')) THEN RAISE EXCEPTION 'Runtime role must not have role memberships'; END IF; END$$;
    GRANT CONNECT ON DATABASE savegeo TO savegeo_runtime;
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    REVOKE CREATE ON SCHEMA public FROM savegeo_runtime;
    GRANT USAGE ON SCHEMA public TO savegeo_runtime;
    GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO savegeo_runtime;
    GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO savegeo_runtime;
    REVOKE ALL ON TABLE alembic_version FROM savegeo_runtime;
    GRANT SELECT ON TABLE alembic_version TO savegeo_runtime;
    ALTER DEFAULT PRIVILEGES FOR ROLE savegeo IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO savegeo_runtime;
    ALTER DEFAULT PRIVILEGES FOR ROLE savegeo IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO savegeo_runtime;
    COMMIT;
    """
    result = subprocess.run(['docker','exec','-i',container,'psql','-v','ON_ERROR_STOP=1','-U','savegeo','-d','savegeo'],
        input=statement, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Runtime role provisioning failed; inspect database locally without printing credentials')
    # Authenticate with the new password over TCP, rather than relying on
    # PostgreSQL's local administrative trust connection used for provisioning.
    verification = """SELECT current_user='savegeo_runtime'
      AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
      AND NOT rolreplication AND NOT rolbypassrls
      AND NOT has_schema_privilege(current_user,'public','CREATE')
      AND has_table_privilege(current_user,'public.alembic_version','SELECT')
      AND NOT has_table_privilege(current_user,'public.alembic_version','UPDATE')
      FROM pg_roles WHERE rolname=current_user;"""
    checked = subprocess.run(
        ['docker','exec','-i',container,'sh','-c',
         'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -h 127.0.0.1 -U savegeo_runtime -d savegeo -v ON_ERROR_STOP=1 -At'],
        input=password+'\n'+verification, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if checked.returncode or checked.stdout.strip() != 't':
        raise RuntimeError('Runtime role authentication or privilege verification failed; environment unchanged')
    url = f'postgresql+psycopg://savegeo_runtime:{quote(password, safe="")}@db:5432/savegeo'
    # The host Apache proxy reaches the API through the Compose bridge gateway.
    # Uvicorn leaves the immediate peer intact; only our trusted-proxy helper
    # interprets X-Forwarded-For. LAN clients cannot reach the loopback binding.
    inspected = subprocess.run(['docker','inspect',container,'--format','{{json .NetworkSettings.Networks}}'],
                               check=True, capture_output=True, text=True)
    networks = json.loads(inspected.stdout)
    gateways = {item['Gateway'] for item in networks.values() if item.get('Gateway')}
    from ipaddress import ip_address
    proxy_cidrs = ','.join(str(ip_address(value))+'/32' for value in sorted(gateways) if ip_address(value).version == 4)
    if not proxy_cidrs:
        raise RuntimeError('Could not verify the deployment bridge gateway; environment unchanged')
    updates = {'RUNTIME_DATABASE_URL': url, 'ALLOWED_ORIGINS': 'https://savegeo.len.co.id', 'TRUSTED_PROXY_CIDRS': proxy_cidrs}
    # Provision once on the deployment host; runtime never generates a key.
    keyring = values.get('SECRET_ENCRYPTION_KEYS', '')
    active_key = values.get('SECRET_ENCRYPTION_ACTIVE_KEY_ID', '')
    if bool(keyring) != bool(active_key):
        raise RuntimeError('Incomplete existing encryption configuration; environment unchanged')
    if keyring:
        parsed_keys = json.loads(keyring)
        if active_key not in parsed_keys or any(len(base64.b64decode(value, validate=True)) != 32 for value in parsed_keys.values()):
            raise RuntimeError('Invalid existing encryption configuration; environment unchanged')
    else:
        active_key = 'local-'+dt.datetime.now(dt.UTC).strftime('%Y%m%d')
        keyring = json.dumps({active_key: base64.b64encode(secrets.token_bytes(32)).decode()}, separators=(',', ':'))
    updates.update(SECRET_ENCRYPTION_KEYS=keyring, SECRET_ENCRYPTION_ACTIVE_KEY_ID=active_key)
    lines = [line for line in original.splitlines() if line.split('=',1)[0].strip() not in updates]
    replacement = '\n'.join(lines+[key+'='+value for key,value in updates.items()])+'\n'
    backup = directory / ('.env.before-static-remediation-'+dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%S%fZ'))
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf8') as file:
        file.write(original)
    temporary = directory / '.env.audit-tmp'
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf8') as file:
        file.write(replacement)
    temporary.replace(env_file)
    print('Restricted runtime role and environment prepared; original environment retained in a private backup')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--db-container', default='backend-db-1')
    args = parser.parse_args()
    prepare(args.directory, args.db_container)
