# Release Checklist

1. Run `make ci` and ensure green.
2. Verify `.env` and runtime data are not included in commits.
3. Confirm `README.md` setup steps are accurate end-to-end.
4. Build image with `docker compose build`.
5. Smoke test DM flow with a clean `app/data` volume.
6. Update `CHANGELOG.md`.
7. Tag release and publish notes.
