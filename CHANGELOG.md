# Changelog

## Unreleased

- When there are no packages left to check (an empty requirements file, or packages that were all skipped, ignored, or excluded), the text output now says `No packages to check.` instead of `All 0 packages have a wheel`, and the Markdown heading is `### wheelgap: no packages to check.`. The exit code stays 0. (#308)
