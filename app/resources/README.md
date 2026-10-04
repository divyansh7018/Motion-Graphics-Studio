# Bundled application resources

Read-only files that ship **with the source tree**: the default theme JSON, the
built-in project templates, and (later) small fonts or icon files. The path is
available as `AppPaths.resources_dir`.

Runtime data never goes here - projects, cache, previews and output live in the
data root documented in `docs/ARCHITECTURE.md`. Anything placed in this folder is
committed to version control and shipped to every user, so keep it small.
