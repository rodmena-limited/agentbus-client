# EARS Spec: Path Traversal and Receive-Side Memory Spikes

## 1. Security: Path Traversal in Identity Resolution
**Issue:** `_key_from_disk` in `client.py` uses `os.path.join` with an unsanitized `agent` name string from the user or `.agentbus/agent` configuration file. A malicious repository can define `.agentbus/agent: ../operator` to traverse the path and force the client to load the workspace-wide highly privileged `operator.env` key instead of an agent-specific key.
- **Requirement:** When resolving a key from disk, the client shall sanitize the agent name to prevent path traversal (e.g., using `_agent_slug` logic to eliminate `..` and `/`).

## 2. Memory Management (Receive-Side OOM Risks)
**Issue:** While the `_encode_attachments` sending path now prevents loading massive files through size caps, the `attachment()` receive path in `client.py` uses `response.content`, which pulls the entire encrypted payload into memory. It then passes the full ciphertext to `unseal_bytes_with_any`, duplicating the memory footprint.
- **Requirement:** When downloading an attachment, the client shall cap the downloaded size or use streaming APIs (`response.iter_bytes()`) with chunked decryption (where supported by the cryptography layer) or a temporary file to avoid holding the full ciphertext in memory.

## 3. Resilience: `AsyncAgentBus._async_bulkhead` Initialization
**Issue:** `AsyncAgentBus._async_bulkhead` is lazily initialized as an `asyncio.Semaphore` on the instance. `asyncio.Semaphore` binds to the event loop running at the time of creation. If the instance is shared across different loops (or globally instantiated), it will crash with a RuntimeError when accessed from another loop.
- **Requirement:** The async client shall ensure that its asyncio primitives (like Semaphore) are bound strictly to the current running event loop, avoiding cross-loop pollution if the client instance is reused.
