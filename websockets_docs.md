# websockets 15.0.1 — Python WebSocket Library

> Version 15.0.1 of the `websockets` library, released 5 March 2025.  
> This document aggregates documentation, change history, API notes, behavior, compatibility, and other relevant facts.

---

## Table of Contents

1. [Overview & Purpose](#overview--purpose)  
2. [Versioning, Changelog & Release Notes](#versioning-changelog--release-notes)  
   1. [Versioning Policy](#versioning-policy)  
   2. [Changelog & Changes in 15.0.1](#changelog--changes-in-1501)  
   3. [Changes introduced in 15.0 (vs previous versions)](#changes-introduced-in-150)  
   4. [Future / In-development (16.0) notes](#future--in-development-160-notes)  
3. [Compatibility & Requirements](#compatibility--requirements)  
4. [Architecture & Internals](#architecture--internals)  
   1. [Core layers: Sans-I/O, asyncio, threading](#core-layers-sans-io-asyncio-threading)  
   2. [Protocol implementation: frames, handshake, extensions](#protocol-implementation-frames-handshake-extensions)  
   3. [Backpressure, limits, memory management](#backpressure-limits-memory-management)  
   4. [Proxy & networking behavior](#proxy--networking-behavior)  
5. [Public API & Usage](#public-api--usage)  
   1. [Asyncio API: server, client, common patterns](#asyncio-api-server-client-common-patterns)  
   2. [Threading / synchronous API](#threading--synchronous-api)  
   3. [Sans-I/O API](#sans-io-api)  
   4. [Routing (new in 15.0)](#routing-new-in-150)  
   5. [Keepalive / Ping-Pong behavior](#keepalive--ping-pong-behavior)  
   6. [Close / shutdown behavior & customizing close codes](#close--shutdown-behavior--customizing-close-codes)  
   7. [Error handling, exceptions, connection state](#error-handling-exceptions-connection-state)  
6. [Deprecations & Migration Notes](#deprecations--migration-notes)  
7. [Limitations, Known Issues & Security](#limitations-known-issues--security)  
8. [Packaging, Distribution & Deployment](#packaging-distribution--deployment)  
9. [License & Governance](#license--governance)  
10. [Examples & Recipes](#examples--recipes)  
    1. [Echo server / client (asyncio)](#echo-server--client-asyncio)  
    2. [Threading / sync client example](#threading--sync-client-example)  
    3. [Using routing, custom close codes, proxies](#using-routing-custom-close-codes-proxies)  
11. [References & Further Reading](#references--further-reading)

---

## 1. Overview & Purpose

`websockets` is a Python library for building WebSocket servers and clients. Its goals include **correctness**, **simplicity**, **robustness**, and **performance**.

- It implements **RFC 6455** (WebSocket protocol) and **RFC 7692** (permessage-deflate extension)  
- It offers multiple API layers:  
  1. **Asyncio-based** coroutine API (the primary usage)  
  2. **Threading / synchronous** API (for simpler or non-async use cases)  
  3. **Sans-I/O** protocol core (for embedding into third-party frameworks)  
- It handles details like framing, fragmentation, masking, control frames, compression, and connection state transparently.  
- The library is production-ready and widely used.

---

## 2. Versioning, Changelog & Release Notes

### 2.1 Versioning Policy

- The project uses **semantic versioning**: `major.minor.patch`.  
- **Backward compatibility** is a priority. The authors attempt to preserve compatibility for **5 years** after a breaking change.  
- When a public API must change, the change is documented in the changelog.  
- Undocumented (private) APIs may change freely.  
- Major version bumps indicate breaking changes; minor bumps are for feature additions without breaking API; patch versions are for bug fixes.  
- The version info is maintained in `src/websockets/version.py` (variables like `version`, `tag`, `commit`)

### 2.2 Changelog & Changes in 15.0.1

From the official changelog (docs/project/changelog.rst) and GitHub release:

- **15.0.1 (5 March 2025)** — *Bug fix release*  
  - Prevented an exception when exiting the interactive client.

No new features were introduced in 15.0.1; it is strictly a stabilization fix.

### 2.3 Changes introduced in 15.0 (vs prior versions)

15.0 (released 16 February 2025) introduced multiple new behaviors and breaking changes. Key changes:

**Backward-incompatible changes**:  
- Client connections **automatically use SOCKS and HTTP proxies** if configured at the OS or via environment variables. To disable this, one must pass `proxy=None` to `connect()`.  
- The **threading** (synchronous) implementation now enables **keepalive**: it sends Ping frames at intervals, and closes the connection if a Pong is not received — matching behavior from the asyncio side.

**New features / improvements**:  
- Added routing functions: `asyncio.router.route` and `asyncio.router.unix_route` to dispatch connections based on request path.  
- Added support for customizing the `close` code & reason in `Server.close()` calls.  
- Type overloads for the `decode` argument of `Connection.recv()` — improves static typing support.  
- Documentation refreshes and updates to guides.

### 2.4 Future / In-development (16.0) notes

From the changelog:

- **16.0**, currently in development, is planned to require **Python ≥ 3.10**. websockets 15.x is the last series supporting Python 3.9.  
- New planned improvements:  
  - Ability to set **separate limits** for **messages** vs **fragments** with `max_size` parameter (i.e. more granularity)  
  - Support for **HTTP/1.0 proxies**  
  - **Custom close code / reason** support (already added in 15.0) further refined.  
  - Validation & compatibility testing for **Python 3.14**

---

## 3. Compatibility & Requirements

- **Python versions supported**: As of 15.0.1, the library continues to support **Python 3.9** through newer versions. (Note: 16.0 will drop 3.9 support)  
- It is available via PyPI, precompiled wheels for multiple OS/architectures.  
- It is packaged in distributions (e.g. Arch Linux, Ubuntu) under `python-websockets 15.0.1`  
- No external dependencies (for the core library).  
- For SOCKS proxy support (automatically used in 15.0+), the optional third-party library **python-socks** must be installed.

---

## 4. Architecture & Internals

### 4.1 Core layers: Sans-I/O, asyncio, threading

- The **Sans-I/O layer** is a pure protocol implementation that defines framing, state machines, extension negotiation, etc., without any I/O operations. It is intended to be embedded in other systems or frameworks.  
- The **asyncio layer** wraps the Sans-I/O layer using `asyncio` primitives (streams, transports) to handle network I/O. This is the primary mode of usage for servers and clients.  
- The **threading (synchronous) layer** offers a blocking API (suitable for simpler clients or code that does not use `asyncio`). Internally, it either uses a separate event loop thread or adapts the core behavior.

As of version 14.0, the *new asyncio implementation* replaced the legacy one as default, and the legacy one is deprecated.

### 4.2 Protocol implementation: frames, handshake, extensions

- The library supports **fragmented and unfragmented frames**, control frames (Ping, Pong, Close), masking (client-side), and reassembly of messages from frames, per RFC 6455.  
- It supports **permessage-deflate** compression extension (RFC 7692) if negotiated during handshake.  
- It implements handshake logic: HTTP upgrade request/response, extension negotiation, subprotocol negotiation, origin checks, etc.  
- It supports **path-based routing** in 15.0+ (i.e. dispatching incoming WebSocket connections to handlers based on the request path).  
- In 15.0+, the `Server.close()` method can include custom close code and reason.  
- The library enforces maximum sizes (message / fragment) and uses internal buffers to manage efficient memory usage.

### 4.3 Backpressure, limits, memory management

- The library is designed to manage backpressure: if messages arrive faster than your application can process them, the library applies limits and flow-control mechanisms to avoid unbounded memory usage. This is one of its core strengths.  
- There are configurable limits (e.g. `max_size`) to bound how large a message may be.  
- Future versions (16.0) aim to split limits for fragments vs messages for greater control.

### 4.4 Proxy & networking behavior

- Starting from 15.0, **client connections automatically use system HTTP or SOCKS proxies**, if configured (e.g. via environment variables). To disable, pass `proxy=None` to `connect()`.  
- SOCKS proxy support depends on having `python-socks` installed.  
- In future versions, HTTP/1.0 proxies may also be supported.  
- Networking uses standard sockets, TLS support via Python's `ssl` module, etc.

---

## 5. Public API & Usage

Below is a summary of the main API surfaces and how to use them. For full reference, consult the official docs at websockets.readthedocs.io (stable) and GitHub reference code.

### 5.1 Asyncio API: server, client, common patterns

#### Server side

```python
import asyncio
from websockets.asyncio.server import serve

async def handler(websocket, path):
    async for message in websocket:
        await websocket.send(f"Echo: {message}")

async def main():
    async with serve(handler, "localhost", 8765):
        await asyncio.Future()  # run forever

asyncio.run(main())
```

Key points:

* `serve(handler, host, port, **options)` returns a context manager for a server.
* The handler is called with `(websocket, path)` for each new WebSocket connection.
* Inside handler, you can `await websocket.recv()` or `async for message in websocket:` to read messages.
* Use `websocket.send(...)` to send messages.
* You can close a connection by `await websocket.close(code, reason)` or let it close naturally.

Options to `serve(...)` include `max_size`, `max_queue`, `ping_interval`, `ping_timeout`, `close_timeout`, `compression` flags, `origin` checks, subprotocols, etc.

#### Client side (asyncio)

```python
from websockets.asyncio.client import connect

async def client():
    async with connect("ws://localhost:8765") as websocket:
        await websocket.send("Hello")
        reply = await websocket.recv()
        print("Got:", reply)
```

Options to `connect(...)` include timeouts, proxy settings (15.0 behavior), subprotocols, extra headers, etc.

### 5.2 Threading / synchronous API

For code not using `asyncio`:

```python
from websockets.sync.client import connect

def sync_client():
    with connect("ws://localhost:8765") as websocket:
        websocket.send("Hello")
        reply = websocket.recv()
        print("Got:", reply)
```

In this mode:

* `connect()` returns a synchronous `WebSocketClientProtocol` that supports `.send()`, `.recv()`, `.close()` methods.
* Keepalive (Ping/Pong) is enabled in the threading API since 15.0.
* Shutdown and behavior mimic the async side as closely as possible.

### 5.3 Sans-I/O API

For embedding inside frameworks or custom I/O loops, the Sans-I/O API lets you drive the protocol by feeding raw bytes into the protocol object and consuming outgoing bytes to send.

Typical usage:

* Create a protocol object (e.g. `websockets.protocol.WebSocketCommonProtocol` or similar)
* Invoke handshake/start methods
* Feed incoming data into `recv_data()` or `feed_data()`
* Get outgoing frames via `data_to_send()`, etc.
* Process events (messages, control frames) from the protocol's internal queues
* Send more data by calling `send()` methods of the protocol interface

(Exact methods vary in internal packages; refer to source / API reference in docs.)

### 5.4 Routing (new in 15.0)

In 15.0 and onward, you can define **routes** so that different request paths map to different handlers:

```python
from websockets.asyncio.server import serve
from websockets.asyncio.router import Router

async def handler_a(ws, path):
    ...

async def handler_b(ws, path):
    ...

router = Router()
router.route("/a", handler_a)
router.route("/b", handler_b)

async with serve(router, "localhost", 8765):
    ...
```

* `route()` and `unix_route()` dispatch based on HTTP path.
* Useful for building WebSocket servers with multiple endpoints.

### 5.5 Keepalive / Ping-Pong behavior

* The library automatically sends **Ping** frames at intervals (`ping_interval`) and expects **Pong** frames within a timeout (`ping_timeout`).
* If a Pong is not received, it closes the connection.
* In the threading API, this behavior is now activated by default starting 15.0.
* Users can disable pinging (set `ping_interval=None`) or configure timeouts via options.

### 5.6 Close / shutdown behavior & customizing close codes

* You can cleanly close a connection via `await websocket.close(code, reason)` (async) or `.close()` (sync).
* As of 15.0, you can specify **custom close code** and **reason string** when closing (for both server and client).
* On the server side, `Server.close()` also supports passing a close code and reason (i.e. customizing the close of the server socket).

### 5.7 Error handling, exceptions, connection state

* The library defines a hierarchy of exceptions: `ConnectionClosed`, `InvalidHandshake`, `WebSocketException`, etc.
* The `ConnectionClosed` exception is raised when trying to send or receive on a closed connection, with attributes `.code` and `.reason`.
* The `websocket.closed` / `websocket.open` flags can be queried to check state.
* If the peer sends a close frame, the library follows the close handshake: echoing a close if needed, then closing the transport.

---

## 6. Deprecations & Migration Notes

* The **legacy asyncio implementation** (pre-14.0) is deprecated. Using `websockets.legacy` is discouraged, and may lead to warnings or errors.
* Code relying on implicit proxy behavior must adapt because client proxy behavior changed in 15.0: automatic proxy usage is now on by default. You might need to explicitly disable (`proxy=None`) or adjust code.
* Using `websockets.legacy` may break in the context of newer frameworks (e.g. Uvicorn) that expect the new implementation.
* If you depend on features or behaviors that change in 16.0 (dropping Python 3.9, different `max_size` logic), plan migration accordingly.
* In general, check the changelog (docs/project/changelog.rst) carefully when upgrading from older major or minor versions.

---

## 7. Limitations, Known Issues & Security

* **No known direct vulnerabilities** in version 15.0.1 per Snyk's database.
* The library depends on correct usage patterns; misconfiguration of limits or ping settings can lead to resource exhaustion or dropped connections.
* Use correct `max_size` and fragmentation limits to avoid denial-of-service via oversized frames/messages.
* Be cautious about proxy and environment-based behavior — unexpected proxy routing might introduce unpredictability.
* In interactive client mode (e.g. REPL), there was an issue causing an exception on exit; that is addressed in 15.0.1.
* The deprecation of legacy APIs might surface issues in frameworks depending on older internals.
* The library is heavily tested (e.g. uses branch coverage > 100%) but correctness depends on usage following protocol contracts.

---

## 8. Packaging, Distribution & Deployment

* Available via **PyPI**: `pip install websockets==15.0.1`
* Wheels are provided for Linux, macOS, Windows, multiple Python versions.
* Included in Linux distributions (e.g. Arch, Ubuntu) as `python-websockets 15.0.1` packages.
* On Arch: package size ~1.8 MB installed.
* On Ubuntu, the `.deb` and source packages are available; the upstream tarball size was ~ 444 KiB.
* Conda / Anaconda: available under `websockets 15.0.1` in `conda-forge` or `anaconda` channels.

---

## 9. License & Governance

* `websockets` is distributed under the **BSD-3-Clause** license.
* Maintained by the `python-websockets` project on GitHub.
* Contributions, issue tracking, code reviews occur on GitHub.
* Project emphasizes security, testing, and documentation quality.

---

## 10. Examples & Recipes

Below are representative examples for typical usage in 15.0.1.

### 10.1 Echo server / client (asyncio)

```python
# server.py
import asyncio
from websockets.asyncio.server import serve

async def echo(ws, path):
    async for msg in ws:
        await ws.send(msg)

async def main():
    async with serve(echo, "localhost", 8765):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
```

```python
# client.py
import asyncio
from websockets.asyncio.client import connect

async def client():
    async with connect("ws://localhost:8765") as ws:
        await ws.send("Hello server")
        reply = await ws.recv()
        print("Server said:", reply)

if __name__ == "__main__":
    asyncio.run(client())
```

### 10.2 Threading / sync client example

```python
# sync_client.py
from websockets.sync.client import connect

def main():
    with connect("ws://localhost:8765") as ws:
        ws.send("Hello server (sync)")
        reply = ws.recv()
        print("Reply:", reply)

if __name__ == "__main__":
    main()
```

### 10.3 Using routing, custom close codes, proxies

```python
import asyncio
from websockets.asyncio.server import serve
from websockets.asyncio.router import Router

async def handler_a(ws, path):
    await ws.send("You hit /a")

async def handler_b(ws, path):
    await ws.send("You hit /b")
    # Close with custom code and reason
    await ws.close(code=4000, reason="Bye")

router = Router()
router.route("/a", handler_a)
router.route("/b", handler_b)

async def main():
    async with serve(router, "localhost", 8765):
        await asyncio.Future()

asyncio.run(main())
```

Client with explicit proxy disabling:

```python
from websockets.asyncio.client import connect

async def cli():
    # Suppose environment has HTTP_PROXY, but we want to disable it
    async with connect("ws://example.com:1234", proxy=None) as ws:
        await ws.send("Hello via direct")
        print(await ws.recv())
```

---

## 11. References & Further Reading

* Official documentation (stable / latest): websockets.readthedocs.io
* Changelog & version history: `docs/project/changelog.rst` in the GitHub repo
* GitHub releases page for 15.0.1
* Snyk vulnerability report for websockets 15.0.1
* Distribution packaging in Arch, Ubuntu
* Proxy behavior changes and deprecation (Uvicorn discussion)

---
