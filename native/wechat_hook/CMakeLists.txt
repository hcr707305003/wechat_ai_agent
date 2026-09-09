cmake_minimum_required(VERSION 3.23)

project(agent_bridge_wechat_hook LANGUAGES CXX)

include(FetchContent)

set(
  WECHAT_HOOK_UPSTREAM_SOURCE_DIR
  ""
  CACHE PATH
  "Optional path to aixed/WeChat-Hook commit 0223630c5b9f1a1da26d70c0bd9361dd8781f01e"
)

if(NOT WECHAT_HOOK_UPSTREAM_SOURCE_DIR)
  FetchContent_Declare(
    wechat_hook_upstream
    GIT_REPOSITORY https://github.com/aixed/WeChat-Hook.git
    GIT_TAG 0223630c5b9f1a1da26d70c0bd9361dd8781f01e
    GIT_SHALLOW FALSE
  )
  FetchContent_GetProperties(wechat_hook_upstream)
  if(NOT wechat_hook_upstream_POPULATED)
    FetchContent_Populate(wechat_hook_upstream)
  endif()
  set(WECHAT_HOOK_UPSTREAM_SOURCE_DIR "${wechat_hook_upstream_SOURCE_DIR}")
endif()

if(NOT EXISTS "${WECHAT_HOOK_UPSTREAM_SOURCE_DIR}/include/httplib.h")
  message(FATAL_ERROR "Pinned WeChat-Hook source is missing include/httplib.h")
endif()

add_library(
  agent_bridge_wechat_hook_core STATIC
  src/build_fingerprint.cpp
  src/quote_api.cpp
)
target_compile_features(agent_bridge_wechat_hook_core PUBLIC cxx_std_20)
target_include_directories(
  agent_bridge_wechat_hook_core
  PUBLIC
    include
    "${WECHAT_HOOK_UPSTREAM_SOURCE_DIR}/include"
)
target_compile_definitions(agent_bridge_wechat_hook_core PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
target_link_libraries(agent_bridge_wechat_hook_core PUBLIC bcrypt version)

add_library(
  agent_bridge_wechat_hook SHARED
  src/dllmain.cpp
  src/hook_server.cpp
)
target_compile_features(agent_bridge_wechat_hook PRIVATE cxx_std_20)
target_include_directories(
  agent_bridge_wechat_hook
  PRIVATE
    include
    "${WECHAT_HOOK_UPSTREAM_SOURCE_DIR}/include"
)
target_compile_definitions(
  agent_bridge_wechat_hook
  PRIVATE
    CPPHTTPLIB_WIN32
    WIN32_LEAN_AND_MEAN
    NOMINMAX
)
target_link_libraries(agent_bridge_wechat_hook PRIVATE agent_bridge_wechat_hook_core ws2_32)
set_target_properties(agent_bridge_wechat_hook PROPERTIES OUTPUT_NAME "agent_bridge_wechat_hook")

enable_testing()
add_executable(wechat_hook_api_tests tests/quote_api_tests.cpp)
target_link_libraries(wechat_hook_api_tests PRIVATE agent_bridge_wechat_hook_core)
add_test(NAME wechat_hook_api_tests COMMAND wechat_hook_api_tests)

