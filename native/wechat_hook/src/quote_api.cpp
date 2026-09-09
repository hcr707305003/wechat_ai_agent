#include "quote_api.h"

#include <algorithm>
#include <cstddef>
#include <utility>

namespace agent_bridge::wechat_hook {
namespace {

bool HasText(const nlohmann::json& object, const char* key) {
    return object.contains(key) && object[key].is_string()
        && !object[key].get_ref<const std::string&>().empty();
}

nlohmann::json FileJson(const FileFingerprint& value) {
    return {
        {"path", value.path},
        {"version", value.version},
        {"sha256", value.sha256},
        {"size", value.size},
    };
}

}  // namespace

QuoteApi::QuoteApi(std::string token, BuildFingerprint fingerprint)
    : token_(std::move(token)), fingerprint_(std::move(fingerprint)) {
    if (token_.empty()) {
        throw std::invalid_argument("Hook token is required");
    }
}

bool QuoteApi::IsAuthorized(const std::string& supplied_token) const noexcept {
    if (supplied_token.size() != token_.size()) {
        return false;
    }
    unsigned char difference = 0;
    for (std::size_t index = 0; index < token_.size(); ++index) {
        difference |= static_cast<unsigned char>(supplied_token[index] ^ token_[index]);
    }
    return difference == 0;
}

ApiResponse QuoteApi::Error(int status_code, std::string code) {
    return {status_code, {{"status", "rejected"}, {"detail", std::move(code)}}};
}

ApiResponse QuoteApi::Status(const std::string& supplied_token) const {
    if (!IsAuthorized(supplied_token)) {
        return Error(401, "unauthorized");
    }
    return {
        200,
        {
            {"service", "agent_bridge_wechat_hook"},
            {"api_version", 1},
            {"client_version", fingerprint_.executable.version},
            {"fingerprint_supported", fingerprint_.supported},
            {"fingerprint",
             {
                 {"executable", FileJson(fingerprint_.executable)},
                 {"module", FileJson(fingerprint_.module)},
             }},
            {"capabilities", {{"quote", false}}},
        },
    };
}

ApiResponse QuoteApi::SendQuote(
    const std::string& supplied_token,
    const std::string& request_body
) {
    if (!IsAuthorized(supplied_token)) {
        return Error(401, "unauthorized");
    }
    const auto body = nlohmann::json::parse(request_body, nullptr, false);
    if (body.is_discarded() || !body.is_object()) {
        return Error(400, "invalid_json");
    }
    if (!HasText(body, "request_id") || !HasText(body, "to_wxid")
        || !HasText(body, "content") || !body.contains("reference")
        || !body["reference"].is_object()) {
        return Error(400, "invalid_request");
    }
    const auto& reference = body["reference"];
    if (!HasText(reference, "conversation_id") || !HasText(reference, "sender_wxid")
        || (!HasText(reference, "server_id") && !HasText(reference, "local_id"))) {
        return Error(400, "invalid_reference");
    }

    const auto request_id = body["request_id"].get<std::string>();
    std::scoped_lock lock(request_mutex_);
    const auto prior = completed_requests_.find(request_id);
    if (prior != completed_requests_.end()) {
        return {200, prior->second};
    }

    nlohmann::json result = {
        {"status", "rejected"},
        {"request_id", request_id},
        {"detail", "unsupported_weixin_build"},
    };
    completed_requests_.emplace(request_id, result);
    return {200, std::move(result)};
}

}  // namespace agent_bridge::wechat_hook

