-- Local development only (compose.yaml). The migrations grant to these roles
-- but don't create them; in production the `infrastructure` repo does.
create role pipeline_rw login password 'pipeline_rw_local';
create role web_ro login password 'web_ro_local';
