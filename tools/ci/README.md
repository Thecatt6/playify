# Continuous integration

`github-workflow.yml` is the CI definition: pytest on Python 3.12 (with real
`ffmpeg`) plus the jsdom suite for the panel. It lives here because the GitHub
token used by the agent that wrote it is not allowed to create files under
`.github/workflows/` (the API answers `Resource not accessible by integration`).

Enable it once, from a checkout with push rights:

```bash
mkdir -p .github/workflows
cp tools/ci/github-workflow.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -m "ci: run pytest and the web ui suite on every push"
git push
```

Running the same two jobs by hand:

```bash
python -m pytest -q -p no:cacheprovider
cd tests/ui && npm install && npm test
```
