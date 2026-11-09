import launch

# requests-cache >= 1.0 is required, not just preferred. The extension relies
# on ignored_parameters both dropping api_key/user_id from the cache key AND
# redacting them from the stored request record. That redaction behaviour
# landed in the 1.x line; 0.9.x and earlier can leave credentials sitting in
# the sqlite cache, which the README promises they won't.
#
# A plain is_installed() check is not enough: upstream's install.py pins no
# version, so anyone who installed the original already has whatever
# requests-cache was resolved then - often 0.9.x - and is_installed() reports
# True for it. So check the actual installed version and upgrade when it is
# below 1.0 (or absent).
_NEEDS_CACHE = True
if launch.is_installed("requests-cache"):
    try:
        from importlib.metadata import version as _pkg_version

        _installed = _pkg_version("requests-cache")
        # Compare numerically where possible; fall back to a string check.
        try:
            _major = int(_installed.split(".")[0])
            _NEEDS_CACHE = _major < 1
        except (ValueError, IndexError):
            _NEEDS_CACHE = not _installed.startswith("1.")
        if _NEEDS_CACHE:
            print(
                f"Ranbooru: requests-cache {_installed} is installed but >=1.0 is "
                "required for credential-safe caching; upgrading."
            )
    except Exception:
        # If the version can't be read, upgrade to be safe rather than risk a
        # pre-1.0 install silently caching credentials.
        _NEEDS_CACHE = True

if _NEEDS_CACHE:
    launch.run_pip(
        'install "requests-cache>=1.0"',
        "requests-cache>=1.0",
    )
