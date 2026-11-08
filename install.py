import launch

# Pin a minimum requests-cache version. The extension relies on
# ignored_parameters both dropping api_key/user_id from the cache key AND
# redacting them from the stored request record; that redaction behaviour is
# present in the 1.x line. Older releases may leave credentials in the sqlite
# cache, which the README promises they won't.
if not launch.is_installed("requests-cache"):
    launch.run_pip('install "requests-cache>=1.0"', "requests-cache>=1.0")
