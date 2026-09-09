# Agent Bridge WeChat Hook adapter

This directory is an isolated, default-off adapter derived from the architecture
of [`aixed/WeChat-Hook`](https://github.com/aixed/WeChat-Hook), pinned to commit
`0223630c5b9f1a1da26d70c0bd9361dd8781f01e`.

It deliberately does **not** produce a `version.dll` proxy and does not install
anything into WeChat. The current milestone provides a loopback-only,
token-authenticated HTTP service with `/status` and `/SendQuoteMsg`. The quote
route is fail-closed and returns `unsupported_weixin_build` until the exact
4.1.12.55 internal call contract has been independently recovered and tested.

Build without loading the DLL:

```powershell
cmake -S native/wechat_hook -B artifacts/wechat_hook_build `
  -A x64 `
  -DWECHAT_HOOK_UPSTREAM_SOURCE_DIR=C:/path/to/pinned/WeChat-Hook
cmake --build artifacts/wechat_hook_build --config Release
ctest --test-dir artifacts/wechat_hook_build -C Release --output-on-failure
```

Exported lifecycle functions are `AgentBridgeHookStart` and
`AgentBridgeHookStop`. Nothing starts from `DllMain`, so merely loading the DLL
cannot open a listener or mutate WeChat.

