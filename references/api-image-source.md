# Execution workflow source

This skill's API execution workflow follows [api-image](https://github.com/yc-duan/api-image), licensed under MIT; see [LICENSE](api-image-LICENSE). Source revision reviewed: `5d53535e78ab99e17b2aad84d6f01d9e7e6c37cf`.

The execution entry point remains this project's `scripts/image_api.py`, which uses `IMAGE_API_BASE_URL` and `IMAGE_API_KEY` from `.env.local` or the process environment. It does not read Codex authentication files or invoke the reference project's CLI.
