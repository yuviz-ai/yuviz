Implement a CI/CD pipeline for the yuviz repo.

Context: there is currently no GitHub Actions (or other CI) config. Local deploy is via ./deployment/sh/dev.sh + deployment/docker/docker-compose.yml. Checks people run manually today include pytest, gateway ctest, and admin-ui npm lint/build.

Goal: a practical CI/CD pipeline so PRs get automated checks and we have a clear path to build/publish/deploy artifacts as appropriate for this codebase.
