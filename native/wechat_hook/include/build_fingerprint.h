#pragma once

#include <cstdint>
#include <string>

namespace agent_bridge::wechat_hook {

struct FileFingerprint {
    std::string path;
    std::string version;
    std::string sha256;
    std::uint64_t size = 0;
};

struct BuildFingerprint {
    FileFingerprint executable;
    FileFingerprint module;
    bool supported = false;
};

BuildFingerprint DetectCurrentBuild();

}  // namespace agent_bridge::wechat_hook

