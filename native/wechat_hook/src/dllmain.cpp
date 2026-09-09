#include "hook_server.h"

#include <Windows.h>

#include <cstdint>
namespace {

agent_bridge::wechat_hook::HookServer& Server() {
    // Keep teardown out of the Windows loader lock. The host must call the
    // explicit stop export before unloading this experimental adapter.
    static auto* server = new agent_bridge::wechat_hook::HookServer();
    return *server;
}

}  // namespace

extern "C" __declspec(dllexport) BOOL AgentBridgeHookStart(
    const char* token,
    std::uint16_t port
) noexcept {
    try {
        return token != nullptr && Server().Start(token, port) ? TRUE : FALSE;
    } catch (...) {
        return FALSE;
    }
}

extern "C" __declspec(dllexport) void AgentBridgeHookStop() noexcept {
    Server().Stop();
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
    }
    return TRUE;
}
