// GET /health is answered by the WebSocket service thread; audio WebSocket upgrades still work.

#include "config/Config.h"
#include "logging/Logger.h"
#include "websocket/WebSocketServer.h"

#include <gtest/gtest.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <string>
#include <thread>

namespace {

uint16_t free_port() {
    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family      = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    socklen_t len = sizeof(addr);
    ::getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &len);
    ::close(fd);
    return ntohs(addr.sin_port);
}

// Sends one raw request and returns everything read until close or a 3 s timeout.
std::string roundtrip(uint16_t port, const std::string& request, bool stop_after_headers = false) {
    const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    timeval tv{3, 0};
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port   = htons(port);
    ::inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        ::close(fd);
        return {};
    }
    ::send(fd, request.data(), request.size(), 0);
    std::string out;
    char buf[1024];
    for (ssize_t n; (n = ::recv(fd, buf, sizeof(buf), 0)) > 0;) {
        out.append(buf, static_cast<size_t>(n));
        if (stop_after_headers && out.find("\r\n\r\n") != std::string::npos) break;
    }
    ::close(fd);
    return out;
}

std::string get(uint16_t port, const std::string& path) {
    return roundtrip(port, "GET " + path + " HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
}

struct Server {
    voiceai::WebSocketConfig cfg;
    voiceai::Logger logger = voiceai::Logger::make_null();
    std::unique_ptr<voiceai::WebSocketServer> server;

    Server() {
        cfg.host = "127.0.0.1";
        cfg.port = free_port();
        server   = std::make_unique<voiceai::WebSocketServer>(cfg, logger);
    }
    ~Server() { server->stop(); }
};

}  // namespace

TEST(WebSocketHealthTest, HealthReturns200) {
    Server s;
    ASSERT_TRUE(s.server->start());
    const auto start = std::chrono::steady_clock::now();
    const std::string resp = get(s.cfg.port, "/health");
    EXPECT_LT(std::chrono::steady_clock::now() - start, std::chrono::seconds(2));
    EXPECT_EQ(resp.rfind("HTTP/1.1 200", 0), 0u) << resp;
    EXPECT_NE(resp.find("ok"), std::string::npos) << resp;
}

TEST(WebSocketHealthTest, OtherPathsGet404InsteadOfHanging) {
    Server s;
    ASSERT_TRUE(s.server->start());
    const auto start = std::chrono::steady_clock::now();
    const std::string resp = get(s.cfg.port, "/anything");
    EXPECT_LT(std::chrono::steady_clock::now() - start, std::chrono::seconds(2));
    EXPECT_EQ(resp.rfind("HTTP/1.1 404", 0), 0u) << resp;
}

TEST(WebSocketHealthTest, AudioWebSocketUpgradeStillConnects) {
    Server s;
    std::atomic<bool> connected{false};
    s.server->set_on_connect([&](auto) { connected = true; });
    ASSERT_TRUE(s.server->start());
    ASSERT_EQ(get(s.cfg.port, "/health").rfind("HTTP/1.1 200", 0), 0u);

    const std::string resp = roundtrip(s.cfg.port,
        "GET /voice/test HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: audio.drachtio.org\r\n\r\n", true);
    EXPECT_EQ(resp.rfind("HTTP/1.1 101", 0), 0u) << resp;
    for (int i = 0; i < 100 && !connected; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(10));
    EXPECT_TRUE(connected);
}
