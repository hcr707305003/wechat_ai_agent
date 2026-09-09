#pragma once

#include "build_fingerprint.h"
#include "json.hpp"

#include <mutex>
#include <string>
#include <unordered_map>

namespace agent_bridge::wechat_hook {

struct ApiResponse {
    int status_code;
    nlohmann::json body;
};

class QuoteApi final {
public:
    QuoteApi(std::string token, BuildFingerprint fingerprint);

    ApiResponse Status(const std::string& supplied_token) const;
    ApiResponse SendQuote(
        const std::string& supplied_token,
        const std::string& request_body
    );

private:
    bool IsAuthorized(const std::string& supplied_token) const noexcept;
    static ApiResponse Error(int status_code, std::string code);

    std::string token_;
    BuildFingerprint fingerprint_;
    std::mutex request_mutex_;
    std::unordered_map<std::string, nlohmann::json> completed_requests_;
};

}  // namespace agent_bridge::wechat_hook

