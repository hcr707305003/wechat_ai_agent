#include "build_fingerprint.h"

#include <Windows.h>
#include <bcrypt.h>

#include <array>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <vector>

namespace agent_bridge::wechat_hook {
namespace {

constexpr auto kExpectedVersion = "4.1.12.55";
constexpr auto kExpectedExeSha256 =
    "BB301EB25B9748D471D8A7E5FB142F6E63B4BF2ECC2D39346E73A902EBA5C135";
constexpr auto kExpectedModuleSha256 =
    "4D92C4C381A8CECA4C591FEA7894298674F89A800A2D3C81D946014AA8524DEE";
constexpr std::uint64_t kExpectedExeSize = 3'127'848;
constexpr std::uint64_t kExpectedModuleSize = 194'903'080;

std::string WideToUtf8(const std::wstring& value) {
    if (value.empty()) {
        return {};
    }
    const int size = WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr
    );
    std::string result(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), size, nullptr, nullptr
    );
    return result;
}

std::string FileVersion(const std::filesystem::path& path) {
    DWORD ignored = 0;
    const DWORD size = GetFileVersionInfoSizeW(path.c_str(), &ignored);
    if (size == 0) {
        return {};
    }
    std::vector<std::byte> buffer(size);
    if (!GetFileVersionInfoW(path.c_str(), 0, size, buffer.data())) {
        return {};
    }
    VS_FIXEDFILEINFO* info = nullptr;
    UINT info_size = 0;
    if (!VerQueryValueW(buffer.data(), L"\\", reinterpret_cast<void**>(&info), &info_size)
        || info == nullptr || info_size < sizeof(VS_FIXEDFILEINFO)) {
        return {};
    }
    std::ostringstream stream;
    stream << HIWORD(info->dwFileVersionMS) << '.' << LOWORD(info->dwFileVersionMS)
           << '.' << HIWORD(info->dwFileVersionLS) << '.' << LOWORD(info->dwFileVersionLS);
    return stream.str();
}

std::string Sha256(const std::filesystem::path& path) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    DWORD hash_object_size = 0;
    DWORD result_size = 0;
    std::vector<UCHAR> hash_object;
    std::array<UCHAR, 32> digest{};

    auto cleanup = [&]() noexcept {
        if (hash != nullptr) {
            BCryptDestroyHash(hash);
        }
        if (algorithm != nullptr) {
            BCryptCloseAlgorithmProvider(algorithm, 0);
        }
    };
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0
        || BCryptGetProperty(
               algorithm,
               BCRYPT_OBJECT_LENGTH,
               reinterpret_cast<PUCHAR>(&hash_object_size),
               sizeof(hash_object_size),
               &result_size,
               0
           ) < 0) {
        cleanup();
        throw std::runtime_error("Unable to initialize SHA-256");
    }
    hash_object.resize(hash_object_size);
    if (BCryptCreateHash(
            algorithm,
            &hash,
            hash_object.data(),
            static_cast<ULONG>(hash_object.size()),
            nullptr,
            0,
            0
        ) < 0) {
        cleanup();
        throw std::runtime_error("Unable to create SHA-256 hash");
    }

    std::ifstream input(path, std::ios::binary);
    if (!input) {
        cleanup();
        throw std::runtime_error("Unable to open fingerprint target");
    }
    std::array<char, 1 << 20> buffer{};
    while (input) {
        input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const auto count = input.gcount();
        if (count > 0 && BCryptHashData(
                hash,
                reinterpret_cast<PUCHAR>(buffer.data()),
                static_cast<ULONG>(count),
                0
            ) < 0) {
            cleanup();
            throw std::runtime_error("Unable to update SHA-256 hash");
        }
    }
    if (BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) < 0) {
        cleanup();
        throw std::runtime_error("Unable to finalize SHA-256 hash");
    }
    cleanup();

    std::ostringstream stream;
    stream << std::uppercase << std::hex << std::setfill('0');
    for (const auto value : digest) {
        stream << std::setw(2) << static_cast<unsigned>(value);
    }
    return stream.str();
}

FileFingerprint Fingerprint(const std::filesystem::path& path) {
    FileFingerprint result;
    result.path = WideToUtf8(path.wstring());
    if (!std::filesystem::is_regular_file(path)) {
        return result;
    }
    result.version = FileVersion(path);
    result.size = std::filesystem::file_size(path);
    result.sha256 = Sha256(path);
    return result;
}

}  // namespace

BuildFingerprint DetectCurrentBuild() {
    std::vector<wchar_t> path_buffer(32768);
    const DWORD length = GetModuleFileNameW(
        nullptr, path_buffer.data(), static_cast<DWORD>(path_buffer.size())
    );
    if (length == 0 || length >= path_buffer.size()) {
        return {};
    }
    const std::filesystem::path executable(
        std::wstring(path_buffer.data(), static_cast<std::size_t>(length))
    );
    std::filesystem::path module;
    const HMODULE module_handle = GetModuleHandleW(L"Weixin.dll");
    if (module_handle != nullptr) {
        const DWORD module_length = GetModuleFileNameW(
            module_handle,
            path_buffer.data(),
            static_cast<DWORD>(path_buffer.size())
        );
        if (module_length > 0 && module_length < path_buffer.size()) {
            module = std::filesystem::path(
                std::wstring(path_buffer.data(), static_cast<std::size_t>(module_length))
            );
        }
    }
    if (module.empty()) {
        module = executable.parent_path() / L"Weixin.dll";
    }
    BuildFingerprint result{Fingerprint(executable), Fingerprint(module), false};
    result.supported =
        result.executable.version == kExpectedVersion
        && result.module.version == kExpectedVersion
        && result.executable.size == kExpectedExeSize
        && result.module.size == kExpectedModuleSize
        && result.executable.sha256 == kExpectedExeSha256
        && result.module.sha256 == kExpectedModuleSha256;
    return result;
}

}  // namespace agent_bridge::wechat_hook
