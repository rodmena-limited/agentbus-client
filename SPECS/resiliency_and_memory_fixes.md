# EARS Spec: Resiliency, Memory, and Race Condition Fixes

## 1. Concurrency and Race Conditions
**Issue:** `watch.py` allows `_drain` to be executed concurrently by both the background thread (via `_drain_async`) and the main thread (during `_backoff_and_drain`), causing double-processing of messages and racing cursor updates.
- **Requirement:** While a background drain is running, when a reconnect backoff triggers, the watcher shall acquire the `_drain_lock` before executing any manual drain on the main thread.
- **Requirement:** The watcher shall ensure that the `cursor` attribute is only read and updated under thread-safe synchronization.

## 2. Memory Management (OOM Risks)
**Issue:** `client.py`'s `_encode_attachments` and `attachment` methods read full binary payloads into memory to encode/decode, causing severe OOM risks on large files.
- **Requirement:** When sending an attachment, the client shall stream or efficiently process the attachment data to avoid loading the entire file into memory simultaneously.
- **Requirement:** When reading an encrypted attachment, the client shall decrypt the data in chunks or streaming fashion where possible, avoiding holding the full ciphertext and plaintext in memory simultaneously.

## 3. Resiliency Patterns
**Issue:** `pyproject.toml` directives state `bulkman` and `resilient-circuit` must be used, but `client.py` implements raw `httpx.request` calls without wrappers and only uses `httpx.Limits`.
- **Requirement:** The SDK shall use `resilient-circuit` for HTTP API requests to automatically retry transient network failures (e.g., TransportError, 503).
- **Requirement:** The SDK shall use `bulkman` with `circuit_breaker_enabled=False` to provide an intra-process concurrency bulkhead for all outgoing API requests.
