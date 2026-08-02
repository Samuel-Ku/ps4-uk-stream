#pragma once

#include "HttpClient.h"

#include <memory>

namespace cs {

// HttpClient backed by libcross2d's `Browser` (a synchronous CURL wrapper).
//
// Requires libcross2d Browser.hpp — NOT part of the standalone harness.
// This file is only compiled when the full pPlay build runs.
//
// Browser is NOT thread-safe (one CURL handle, one response buffer), so
// every call from this class is expected to happen on the worker thread
// owned by CatalogApi::Impl. Do not call it from anywhere else.
std::unique_ptr<HttpClient> makeBrowserHttpClient();

} // namespace cs
