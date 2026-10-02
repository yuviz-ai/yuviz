// EslEventListener event subscription and TransferCorrelator wiring, against a minimal
// loopback ESL server that pushes scripted event-plain frames.

#include <gtest/gtest.h>

#include "config/Config.h"
#include "logging/Logger.h"
#include "telephony/EslEventListener.h"
#include "telephony/TransferCorrelator.h"

#include <arpa/inet.h>
#include <atomic>
#include <chrono>
#include <mutex>
#include <netinet/in.h>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>

namespace {

using namespace voiceai;
using namespace std::chrono_literals;

std::string recv_until_blank_line(int fd) {
    std::string buf;
    char chunk[4096];
    while (buf.find("\n\n") == std::string::npos) {
        const ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
        if (n <= 0) break;
        buf.append(chunk, static_cast<size_t>(n));
    }
    const auto pos = buf.find("\n\n");
    return pos == std::string::npos ? buf : buf.substr(0, pos);
}

void send_all(int fd, const std::string& data) {
    ::send(fd, data.data(), data.size(), 0);
}

// Completes auth + subscribe, then holds one client connection open for send_event().
struct FakeEslEventServer {
    int               listen_fd{-1};
    uint16_t          port{0};
    std::thread       server_thread;
    std::atomic<bool> stop{false};
    std::atomic<int>  client_fd{-1};
    std::string       subscribe_command;  // captured for assertions

    void start() {
        listen_fd = ::socket(AF_INET, SOCK_STREAM, 0);
        int opt = 1;
        ::setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

        sockaddr_in addr{};
        addr.sin_family      = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port        = 0;
        ::bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));

        sockaddr_in bound{};
        socklen_t   len = sizeof(bound);
        ::getsockname(listen_fd, reinterpret_cast<sockaddr*>(&bound), &len);
        port = ntohs(bound.sin_port);

        ::listen(listen_fd, 4);
        server_thread = std::thread([this] { run(); });
    }

    void run() {
        sockaddr_in peer{};
        socklen_t   plen = sizeof(peer);
        const int   fd   = ::accept(listen_fd, reinterpret_cast<sockaddr*>(&peer), &plen);
        if (fd < 0) return;

        send_all(fd, "Content-Type: auth/request\n\n");
        const std::string auth_line = recv_until_blank_line(fd);
        if (auth_line != "auth ClueCon") { ::close(fd); return; }
        send_all(fd, "Content-Type: command/reply\nReply-Text: +OK accepted\n\n");

        subscribe_command = recv_until_blank_line(fd);
        send_all(fd, "Content-Type: command/reply\nReply-Text: +OK\n\n");

        client_fd.store(fd);
        while (!stop.load()) std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    // Bounded wait for the handshake so send_event() doesn't race it.
    bool wait_for_client(std::chrono::milliseconds timeout = 2s) {
        const auto deadline = std::chrono::steady_clock::now() + timeout;
        while (client_fd.load() < 0) {
            if (std::chrono::steady_clock::now() >= deadline) return false;
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        return true;
    }

    void send_event(const std::string& event_name, const std::string& uuid) {
        const std::string body  = "Event-Name: " + event_name + "\nUnique-ID: " + uuid + "\n";
        const std::string frame = "Content-Type: text/event-plain\nContent-Length: "
            + std::to_string(body.size()) + "\n\n" + body;
        const int fd = client_fd.load();
        if (fd >= 0) send_all(fd, frame);
    }

    void send_dtmf(const std::string& uuid, const std::string& digit) {
        const std::string body  = "Event-Name: DTMF\nUnique-ID: " + uuid + "\nDTMF-Digit: " + digit + "\n";
        const std::string frame = "Content-Type: text/event-plain\nContent-Length: "
            + std::to_string(body.size()) + "\n\n" + body;
        const int fd = client_fd.load();
        if (fd >= 0) send_all(fd, frame);
    }

    // BACKGROUND_JOB is doubly framed: no Unique-ID, an inner header block, then result text.
    void send_background_job(const std::string& job_uuid, const std::string& result_text) {
        const std::string inner_headers =
            "Event-Name: BACKGROUND_JOB\nJob-UUID: " + job_uuid + "\n";
        const std::string body = inner_headers + "\n" + result_text;
        const std::string frame = "Content-Type: text/event-plain\nContent-Length: "
            + std::to_string(body.size()) + "\n\n" + body;
        const int fd = client_fd.load();
        if (fd >= 0) send_all(fd, frame);
    }

    void stop_server() {
        stop.store(true);
        const int fd = client_fd.load();
        if (fd >= 0) ::shutdown(fd, SHUT_RDWR);
        if (listen_fd >= 0) { ::shutdown(listen_fd, SHUT_RDWR); ::close(listen_fd); }
        if (server_thread.joinable()) server_thread.join();
        if (fd >= 0) ::close(fd);
    }

    ~FakeEslEventServer() { stop_server(); }
};

EslConfig make_cfg(uint16_t port) {
    EslConfig cfg;
    cfg.enabled            = true;
    cfg.host               = "127.0.0.1";
    cfg.port               = port;
    cfg.password           = "ClueCon";
    cfg.connect_timeout_ms = 500;
    return cfg;
}

// Polls `pred` until true or `timeout` elapses (avoids fixed sleeps).
template <typename Pred>
bool wait_until(Pred pred, std::chrono::milliseconds timeout = 2s) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (!pred()) {
        if (std::chrono::steady_clock::now() >= deadline) return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return true;
}

const EslEventListener::DtmfHandler kNoDtmf = [](const std::string&, const std::string&) {};

} // namespace

TEST(EslEventListenerTest, SubscribesToBothChannelHangupAndChannelBridge) {
    FakeEslEventServer server;
    server.start();

    Logger logger = Logger::make_null();
    TransferCorrelator correlator;
    TransferCorrelator job_correlator;
    EslEventListener listener{make_cfg(server.port), logger,
                              [](const std::string&) {}, kNoDtmf, correlator, job_correlator};
    ASSERT_TRUE(listener.start());
    ASSERT_TRUE(server.wait_for_client());

    EXPECT_EQ(server.subscribe_command,
              "event plain CHANNEL_HANGUP CHANNEL_BRIDGE CHANNEL_ANSWER BACKGROUND_JOB DTMF");
    listener.stop();
}

TEST(EslEventListenerTest, ChannelBridgeResolvesPendingTransferAsSuccess) {
    FakeEslEventServer server;
    server.start();

    Logger logger = Logger::make_null();
    TransferCorrelator correlator;
    TransferCorrelator job_correlator;
    std::atomic<bool> hangup_fired{false};
    EslEventListener listener{make_cfg(server.port), logger,
                              [&](const std::string&) { hangup_fired.store(true); },
                              kNoDtmf, correlator, job_correlator};
    ASSERT_TRUE(listener.start());
    ASSERT_TRUE(server.wait_for_client());

    std::atomic<bool> resolved{false};
    std::atomic<bool> resolved_success{false};
    correlator.watch("transfer-uuid-1", [&](bool success, std::string) {
        resolved_success.store(success);
        resolved.store(true);
    });

    server.send_event("CHANNEL_BRIDGE", "transfer-uuid-1");

    ASSERT_TRUE(wait_until([&] { return resolved.load(); }));
    EXPECT_TRUE(resolved_success.load());
    EXPECT_FALSE(hangup_fired.load());  // CHANNEL_BRIDGE never drives on_hangup

    listener.stop();
}

TEST(EslEventListenerTest, ChannelHangupResolvesPendingTransferAsFailureAndSkipsGenericHangup) {
    FakeEslEventServer server;
    server.start();

    Logger logger = Logger::make_null();
    TransferCorrelator correlator;
    TransferCorrelator job_correlator;
    std::atomic<bool> hangup_fired{false};
    EslEventListener listener{make_cfg(server.port), logger,
                              [&](const std::string&) { hangup_fired.store(true); },
                              kNoDtmf, correlator, job_correlator};
    ASSERT_TRUE(listener.start());
    ASSERT_TRUE(server.wait_for_client());

    std::atomic<bool> resolved{false};
    std::atomic<bool> resolved_success{true};
    std::string       detail;
    std::mutex        detail_mutex;
    correlator.watch("transfer-uuid-2", [&](bool success, std::string d) {
        resolved_success.store(success);
        { std::lock_guard lock{detail_mutex}; detail = std::move(d); }
        resolved.store(true);
    });

    server.send_event("CHANNEL_HANGUP", "transfer-uuid-2");

    ASSERT_TRUE(wait_until([&] { return resolved.load(); }));
    EXPECT_FALSE(resolved_success.load());
    {
        std::lock_guard lock{detail_mutex};
        EXPECT_EQ(detail, "hangup_before_bridge");
    }
    // Consumed by transfer resolution; the generic hangup callback must not fire.
    EXPECT_FALSE(hangup_fired.load());

    listener.stop();
}

TEST(EslEventListenerTest, ChannelHangupWithNoPendingTransferFiresGenericHangupHandler) {
    FakeEslEventServer server;
    server.start();

    Logger logger = Logger::make_null();
    TransferCorrelator correlator;
    TransferCorrelator job_correlator;
    std::atomic<bool> hangup_fired{false};
    std::string        hangup_uuid;
    std::mutex         uuid_mutex;
    EslEventListener listener{make_cfg(server.port), logger,
                              [&](const std::string& uuid) {
                                  { std::lock_guard lock{uuid_mutex}; hangup_uuid = uuid; }
                                  hangup_fired.store(true);
                              },
                              kNoDtmf, correlator, job_correlator};
    ASSERT_TRUE(listener.start());
    ASSERT_TRUE(server.wait_for_client());

    // No watch registered: an ordinary caller hangup.
    server.send_event("CHANNEL_HANGUP", "ordinary-call-uuid");

    ASSERT_TRUE(wait_until([&] { return hangup_fired.load(); }));
    {
        std::lock_guard lock{uuid_mutex};
        EXPECT_EQ(hangup_uuid, "ordinary-call-uuid");
    }

    listener.stop();
}

TEST(EslEventListenerTest, ChannelBridgeWithNoPendingTransferIsIgnored) {
    FakeEslEventServer server;
    server.start();

    Logger logger = Logger::make_null();
    TransferCorrelator correlator;
    TransferCorrelator job_correlator;
    std::atomic<bool> hangup_fired{false};
    EslEventListener listener{make_cfg(server.port), logger,
                              [&](const std::string&) { hangup_fired.store(true); },
                              kNoDtmf, correlator, job_correlator};
    ASSERT_TRUE(listener.start());
    ASSERT_TRUE(server.wait_for_client());

    server.send_event("CHANNEL_BRIDGE", "unrelated-uuid");
    // Sync marker: proves the BRIDGE above was processed and ignored.
    server.send_event("CHANNEL_HANGUP", "sync-marker-uuid");

    ASSERT_TRUE(wait_until([&] { return hangup_fired.load(); }));
    listener.stop();
}

// ── BACKGROUND_JOB: success result text is "+OK <uuid>", not a bare uuid ──

TEST(EslEventListenerTest, BackgroundJobSuccessStripsOkPrefixToBareUuid) {
    FakeEslEventServer server;
    server.start();

    Logger logger = Logger::make_null();
    TransferCorrelator correlator;
    TransferCorrelator job_correlator;
    EslEventListener listener{make_cfg(server.port), logger,
                              [](const std::string&) {}, kNoDtmf, correlator, job_correlator};
    ASSERT_TRUE(listener.start());
    ASSERT_TRUE(server.wait_for_client());

    std::atomic<bool> resolved{false};
    std::atomic<bool> resolved_success{false};
    std::string       detail;
    std::mutex        detail_mutex;
    job_correlator.watch("job-uuid-1", [&](bool success, std::string d) {
        resolved_success.store(success);
        { std::lock_guard lock{detail_mutex}; detail = std::move(d); }
        resolved.store(true);
    });

    server.send_background_job("job-uuid-1", "+OK 62b68449-1268-4f0e-a3d7-0cb4fd7f404c");

    ASSERT_TRUE(wait_until([&] { return resolved.load(); }));
    EXPECT_TRUE(resolved_success.load());
    {
        std::lock_guard lock{detail_mutex};
        // Bare uuid only — no leading "+OK " left in what becomes agent_uuid_.
        EXPECT_EQ(detail, "62b68449-1268-4f0e-a3d7-0cb4fd7f404c");
    }

    listener.stop();
}

TEST(EslEventListenerTest, BackgroundJobFailureKeepsErrTextUnstripped) {
    FakeEslEventServer server;
    server.start();

    Logger logger = Logger::make_null();
    TransferCorrelator correlator;
    TransferCorrelator job_correlator;
    EslEventListener listener{make_cfg(server.port), logger,
                              [](const std::string&) {}, kNoDtmf, correlator, job_correlator};
    ASSERT_TRUE(listener.start());
    ASSERT_TRUE(server.wait_for_client());

    std::atomic<bool> resolved{false};
    std::atomic<bool> resolved_success{true};
    std::string       detail;
    std::mutex        detail_mutex;
    job_correlator.watch("job-uuid-2", [&](bool success, std::string d) {
        resolved_success.store(success);
        { std::lock_guard lock{detail_mutex}; detail = std::move(d); }
        resolved.store(true);
    });

    server.send_background_job("job-uuid-2", "-ERR NO_ANSWER");

    ASSERT_TRUE(wait_until([&] { return resolved.load(); }));
    EXPECT_FALSE(resolved_success.load());
    {
        std::lock_guard lock{detail_mutex};
        EXPECT_EQ(detail, "-ERR NO_ANSWER");
    }

    listener.stop();
}

TEST(EslEventListenerTest, DtmfEventReachesTheDtmfHandler) {
    FakeEslEventServer server;
    server.start();

    Logger logger = Logger::make_null();
    TransferCorrelator correlator;
    TransferCorrelator job_correlator;
    std::mutex  got_mutex;
    std::string got_uuid, got_digit;
    std::atomic<bool> fired{false};
    EslEventListener listener{make_cfg(server.port), logger,
                              [](const std::string&) {},
                              [&](const std::string& uuid, const std::string& digit) {
                                  { std::lock_guard lock{got_mutex}; got_uuid = uuid; got_digit = digit; }
                                  fired.store(true);
                              },
                              correlator, job_correlator};
    ASSERT_TRUE(listener.start());
    ASSERT_TRUE(server.wait_for_client());

    server.send_dtmf("call-uuid-7", "5");

    ASSERT_TRUE(wait_until([&] { return fired.load(); }));
    {
        std::lock_guard lock{got_mutex};
        EXPECT_EQ(got_uuid, "call-uuid-7");
        EXPECT_EQ(got_digit, "5");
    }
    listener.stop();
}
