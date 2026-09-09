#include "quote_api.h"

#include <cassert>
#include <string>

using agent_bridge::wechat_hook::BuildFingerprint;
using agent_bridge::wechat_hook::QuoteApi;

namespace {

std::string Request(const std::string& request_id) {
    return R"({
        "to_wxid": "wxid_target",
        "content": "aaa",
        "request_id": ")" + request_id + R"(",
        "reference": {
            "conversation_id": "wxid_target",
            "server_id": "987654321",
            "local_id": "246",
            "sender_wxid": "wxid_target",
            "sender_name": "工藤新一",
            "message_type": 1,
            "content": "引用测试"
        }
    })";
}

}  // namespace

int main() {
    BuildFingerprint fingerprint;
    fingerprint.executable.version = "4.1.12.55";
    QuoteApi api("secret", fingerprint);

    const auto unauthorized = api.Status("wrong");
    assert(unauthorized.status_code == 401);
    assert(unauthorized.body.at("detail") == "unauthorized");

    const auto status = api.Status("secret");
    assert(status.status_code == 200);
    assert(status.body.at("fingerprint_supported") == false);
    assert(status.body.at("capabilities").at("quote") == false);

    const auto invalid = api.SendQuote("secret", "not-json");
    assert(invalid.status_code == 400);
    assert(invalid.body.at("detail") == "invalid_json");

    const auto first = api.SendQuote("secret", Request("request-1"));
    assert(first.status_code == 200);
    assert(first.body.at("status") == "rejected");
    assert(first.body.at("detail") == "unsupported_weixin_build");
    assert(first.body.at("request_id") == "request-1");

    const auto duplicate = api.SendQuote("secret", Request("request-1"));
    assert(duplicate.status_code == 200);
    assert(duplicate.body == first.body);
    return 0;
}

