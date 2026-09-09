#pragma once

#include "quote_api.h"

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace httplib {
class Server;
}

namespace agent_bridge::wechat_hook {

class HookServer final {
public:
    HookServer();
    ~HookServer();

    HookServer(const HookServer&) = delete;
    HookServer& operator=(const HookServer&) = delete;

    bool Start(std::string token, std::uint16_t port);
    void Stop() noexcept;

private:
    std::mutex lifecycle_mutex_;
    std::unique_ptr<QuoteApi> api_;
    std::unique_ptr<httplib::Server> server_;
    std::thread thread_;
};

}  // namespace agent_bridge::wechat_hook
