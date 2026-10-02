// ColdTransferCoordinator contract tests (start/cancel/shutdown/state, callbacks).
// Most use cfg.enabled=false so EslClient::transfer() rejects synchronously without a socket.

#include <gtest/gtest.h>

#include "config/Config.h"
#include "logging/Logger.h"
#include "telephony/ColdTransferCoordinator.h"

#include <arpa/inet.h>
#include <atomic>
#include <netinet/in.h>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>

using namespace voiceai;

namespace {

// Minimal single-exchange fake ESL server for the command-accepted path.
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

struct FakeEslServer {
    int               listen_fd{-1};
    uint16_t          port{0};
    std::thread       server_thread;
    std::atomic<bool> stop{false};

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
        const int fd = ::accept(listen_fd, nullptr, nullptr);
        if (fd < 0) return;

        send_all(fd, "Content-Type: auth/request\n\n");
        recv_until_blank_line(fd);  // "auth ClueCon"
        send_all(fd, "Content-Type: command/reply\nReply-Text: +OK accepted\n\n");

        recv_until_blank_line(fd);  // the uuid_transfer command itself
        const std::string body = "+OK";
        send_all(fd, "Content-Type: api/response\nContent-Length: "
            + std::to_string(body.size()) + "\n\n" + body);

        ::close(fd);
    }

    void stop_server() {
        stop.store(true);
        if (listen_fd >= 0) { ::shutdown(listen_fd, SHUT_RDWR); ::close(listen_fd); }
        if (server_thread.joinable()) server_thread.join();
    }

    ~FakeEslServer() { stop_server(); }
};

struct ColdTransferCoordinatorTest : ::testing::Test {
    Logger        logger = Logger::make_null();
    EslConfig     esl_cfg;  // enabled=false by default
    EslClient     esl_client{esl_cfg, logger};
    TransferCorrelator correlator;
    ObservabilityContext obs{"session-1", "tenant-1", "trace-1", "call-uuid-1", ""};
    ContextLogger log{obs, logger};
    ColdTransferCoordinator coordinator{esl_client, correlator, log};
};

TEST_F(ColdTransferCoordinatorTest, IdleStateBeforeStart) {
    EXPECT_EQ(coordinator.state(), CoordinatorState::Idle);
}

TEST_F(ColdTransferCoordinatorTest, DisabledEslResolvesSynchronouslyAsFailure) {
    bool         fired = false;
    bool         success = true;
    std::string  detail;

    TransferCoordinatorCallbacks cbs;
    cbs.on_transfer_completed = [&](bool s, std::string /*dest*/, std::string d) {
        fired   = true;
        success = s;
        detail  = std::move(d);
    };

    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "caller_requested_human", "tid-1"},
        std::move(cbs));

    EXPECT_TRUE(fired);
    EXPECT_FALSE(success);
    EXPECT_EQ(detail, "esl_disabled");
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);
}

TEST_F(ColdTransferCoordinatorTest, CallbackReceivesTheRequestedDestination) {
    std::string seen_destination;
    TransferCoordinatorCallbacks cbs;
    cbs.on_transfer_completed = [&](bool, std::string dest, std::string) {
        seen_destination = std::move(dest);
    };

    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "+18005550100", "x", "tid-2"},
        std::move(cbs));

    EXPECT_EQ(seen_destination, "+18005550100");
}

TEST_F(ColdTransferCoordinatorTest, ShutdownAfterCompletionIsSafeAndIdempotent) {
    TransferCoordinatorCallbacks cbs;
    cbs.on_transfer_completed = [](bool, std::string, std::string) {};
    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "x", "tid-3"}, std::move(cbs));

    ASSERT_EQ(coordinator.state(), CoordinatorState::Completed);
    coordinator.shutdown();  // must not throw, crash, or double-fire anything
    coordinator.shutdown();  // idempotent — safe to call twice
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);
}

TEST_F(ColdTransferCoordinatorTest, ShutdownBeforeStartIsANoOp) {
    coordinator.shutdown();  // must not throw or crash
    EXPECT_EQ(coordinator.state(), CoordinatorState::Idle);
}

TEST_F(ColdTransferCoordinatorTest, CancelIsANoOpAtAnyState) {
    // Cold has no pre-dispatch phase to abort.
    coordinator.cancel();  // before start() — no-op
    EXPECT_EQ(coordinator.state(), CoordinatorState::Idle);

    TransferCoordinatorCallbacks cbs;
    cbs.on_transfer_completed = [](bool, std::string, std::string) {};
    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "x", "tid-4"}, std::move(cbs));

    coordinator.cancel();  // after resolution — still a no-op, not a crash
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);
}

TEST_F(ColdTransferCoordinatorTest, MissingCallbackDoesNotCrash) {
    // No on_transfer_completed set: the coordinator must guard the empty std::function.
    TransferCoordinatorCallbacks cbs;  // on_transfer_completed left unset
    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "x", "tid-5"}, std::move(cbs));
    SUCCEED();
}

// ── on_media_handoff: fires only when uuid_transfer is accepted ──

TEST(ColdTransferCoordinatorAcceptedPathTest, OnMediaHandoffFiresWhenCommandAccepted) {
    FakeEslServer server;
    server.start();

    Logger logger = Logger::make_null();
    EslConfig cfg;
    cfg.enabled            = true;
    cfg.host               = "127.0.0.1";
    cfg.port               = server.port;
    cfg.password            = "ClueCon";
    cfg.connect_timeout_ms  = 500;
    cfg.sip_proxy_host      = "127.0.0.1";  // "1001" is dialed through Kamailio
    EslClient client{cfg, logger};
    TransferCorrelator correlator;
    ObservabilityContext obs{"session-1", "tenant-1", "trace-1", "call-uuid-1", ""};
    ContextLogger log{obs, logger};
    ColdTransferCoordinator coordinator{client, correlator, log};

    bool handoff_fired = false;
    bool completed_fired = false;
    TransferCoordinatorCallbacks cbs;
    cbs.on_media_handoff = [&] { handoff_fired = true; };
    cbs.on_transfer_completed = [&](bool, std::string, std::string) { completed_fired = true; };

    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "x", "tid-6"}, std::move(cbs));

    EXPECT_TRUE(handoff_fired);
    // CHANNEL_BRIDGE hasn't arrived; only the handoff fires on command acceptance.
    EXPECT_FALSE(completed_fired);
    EXPECT_EQ(coordinator.state(), CoordinatorState::Active);

    coordinator.shutdown();
}

TEST_F(ColdTransferCoordinatorTest, OnMediaHandoffDoesNotFireOnImmediateRejection) {
    bool handoff_fired = false;
    TransferCoordinatorCallbacks cbs;
    cbs.on_media_handoff = [&] { handoff_fired = true; };
    cbs.on_transfer_completed = [](bool, std::string, std::string) {};

    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "x", "tid-7"}, std::move(cbs));

    EXPECT_FALSE(handoff_fired);
}

} // namespace
