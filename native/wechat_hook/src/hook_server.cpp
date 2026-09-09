#include "hook_server.h"

#include "httplib.h"

#include <utility>

namespace agent_bridge::wechat_hook {
namespace {

std::string TokenFrom(const httplib::Request& request) {
    return request.get_header_value("X-Agent-Bridge-Token");
}

void WriteResponse(const ApiResponse& source, httplib::Response& target) {
    target.status = source.status_code;
    target.set_content(source.body.dump(), "application/json; charset=utf-8");
}

}  // namespace

HookServer::HookServer() = default;

HookServer::~HookServer() {
    Stop();
}

bool HookServer::Start(std::string token, std::uint16_t port) {
    std::scoped_lock lock(lifecycle_mutex_);
    if (server_ != nullptr || token.empty() || port == 0) {
        return false;
    }

    auto api = std::make_unique<QuoteApi>(std::move(token), DetectCurrentBuild());
    auto server = std::make_unique<httplib::Server>();
    server->Get("/status", [raw_api = api.get()](const auto& request, auto& response) {
        WriteResponse(raw_api->Status(TokenFrom(request)), response);
    });
    server->Post(
        "/SendQuoteMsg",
        [raw_api = api.get()](const auto& request, auto& response) {
            WriteResponse(raw_api->SendQuote(TokenFrom(request), request.body), response);
        }
    );

    if (!server->bind_to_port("127.0.0.1", static_cast<int>(port))) {
        return false;
    }
    api_ = std::move(api);
    server_ = std::move(server);
    thread_ = std::thread([this]() { server_->listen_after_bind(); });
    return true;
}

void HookServer::Stop() noexcept {
    std::scoped_lock lock(lifecycle_mutex_);
    if (server_ == nullptr) {
        return;
    }
    server_->stop();
    if (thread_.joinable()) {
        thread_.join();
    }
    server_.reset();
    api_.reset();
}

}  // namespace agent_bridge::wechat_hook
