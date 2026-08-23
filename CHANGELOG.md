# Changelog

This repository does not maintain a single combined changelog. Each package is
released independently: `.github/workflows/auto-release.yml` computes a
semantic-version bump per package from Conventional Commits, pushes it
straight to main, and tags a GitHub Release per bumped package. See each
package's own Releases (filter tags by `<package-name>-v*`) for its real
history.
