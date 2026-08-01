# v1 deviations (issue #16)

Deviations discovered during the v1 implementation pass (Tasks 1-15 of the
v1 plan). Each one is also noted in the relevant commit and the test
suite; this document is for future contributors so the same gotchas do
not have to be re-discovered.

## 1. OnscreenKeyboard static member qualification

**Symptom**: GCC error inside `OnscreenKeyboard.cpp`:
`error: 'kRows' was not declared in this scope` (and same for `kCols`).

**Cause**: `kLayout` is declared at namespace scope inside an anonymous
namespace, but its type uses `OnscreenKeyboard::kRows` /
`OnscreenKeyboard::kCols` from the enclosing `cs` namespace. Without
explicit qualification, the unqualified names do not resolve from the
anonymous-namespace initializer.

**Fix**: qualify the static constants explicitly in the array type:

```cpp
namespace {
const char *kLayout[OnscreenKeyboard::kRows][OnscreenKeyboard::kCols] = {
    // ...
};
} // namespace
```

Commit: `1b55c56`.

## 2. cJSON C linkage

**Symptom**: Linker errors when building the standalone catalog test
harness:
`undefined reference to cJSON_Parse` (and every other `cJSON_*` symbol).

**Cause**: The standalone CMake project declared `CXX` only, but
`cJSON.c` is a C file. CMake omits C linking when only CXX is
requested, so the C standard library startup and the cJSON symbols
themselves never make it into the link line.

**Fix**: add `C` to the `project()` languages:

```cmake
project(cs_catalog_standalone CXX C)
```

Commit: `454caf1`.

## 3. Uakino id format — keep the `film-` / `serial-` prefix

**Symptom**: Task 5 test assertion said `id == "uakino:dune-2021"`,
but Task 6's `content()` implementation and Task 7's `/api/content`
routing required the `film-` / `serial-` prefix to stay in the id.

**Cause**: The original plan assumed `_external_id_from_url` would
strip the kind prefix. In practice, `content()` uses
`external_id.partition("-")` to route to `/film/...` or `/serial/...`,
so the prefix is load-bearing.

**Fix**: keep the prefix and update the test:

```python
# tests/test_uakino.py
assert results[0].id == "uakino:film-dune-2021"   # not "uakino:dune-2021"
```

This is the working contract — `search()` and `content()` agree on the
format, and `/api/content/uakino:film-dune-2021` routes correctly.

Commits: `5ca1ee2`, `fcb4a40`.

## What this means for the v2 plan rewrite

These are the only behavioural deviations in the v1 pass that any
future plan needs to respect. The `#12` rewrite folds them in as known
gotchas.