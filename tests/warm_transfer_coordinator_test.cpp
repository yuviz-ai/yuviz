// WarmTransferCoordinator: state machine with ESL disabled, and command
// sequencing against a scripted fake ESL server (wire format is in esl_client_test.cpp).

#include <gtest/gtest.h>

#include "config/Config.h"
#include "logging/Logger.h"
#include "telephony/WarmTransferCoordinator.h"

#include <arpa/inet.h>
#include <atomic>
#include <netinet/in.h>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>

using namespace voiceai;

namespace {

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

std::string api_response_frame(const std::string& body) {
    return "Content-Type: api/response\nContent-Length: "
        + std::to_string(body.size()) + "\n\n" + body;
}

std::string bgapi_reply_frame(const std::string& job_uuid) {
    return "Content-Type: command/reply\nReply-Text: +OK Job-UUID: "
        + job_uuid + "\nJob-UUID: " + job_uuid + "\n\n";
}

// One persistent connection (EslClient reuses its fd), one scripted reply per command.
struct MultiCommandFakeEslServer {
    int              listen_fd{-1};
    uint16_t         port{0};
    std::thread      server_thread;
    std::atomic<bool> stop{false};

    std::vector<std::string> scripted_replies;   // fully framed, sent in order
    std::vector<std::string> received_commands;

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
        recv_until_blank_line(fd);  // "auth ClueCon"
        send_all(fd, "Content-Type: command/reply\nReply-Text: +OK accepted\n\n");

        for (const auto& reply : scripted_replies) {
            if (stop.load()) break;
            const std::string command = recv_until_blank_line(fd);
            received_commands.push_back(command);
            send_all(fd, reply);
        }
        ::close(fd);
    }

    void stop_server() {
        stop.store(true);
        if (listen_fd >= 0) ::shutdown(listen_fd, SHUT_RDWR);
        if (listen_fd >= 0) ::close(listen_fd);
        if (server_thread.joinable()) server_thread.join();
    }

    ~MultiCommandFakeEslServer() { stop_server(); }
};

EslConfig make_cfg(uint16_t port) {
    EslConfig cfg;
    cfg.enabled            = true;
    cfg.host               = "127.0.0.1";
    cfg.port               = port;
    cfg.password            = "ClueCon";
    cfg.connect_timeout_ms  = 500;
    cfg.sip_proxy_host      = "127.0.0.1";  // Kamailio on this host (SIP_IP=127.0.0.1)
    return cfg;
}

struct DisabledWarmTransferCoordinatorTest : ::testing::Test {
    Logger              logger = Logger::make_null();
    EslConfig           esl_cfg;  // enabled=false by default
    EslClient           esl_client{esl_cfg, logger};
    TransferCorrelator  leg_correlator;
    TransferCorrelator  job_correlator;
    ObservabilityContext obs{"session-1", "tenant-1", "trace-1", "call-uuid-1", ""};
    ContextLogger       log{obs, logger};
    WarmTransferCoordinator coordinator{esl_client, leg_correlator, job_correlator, log};
};

TEST_F(DisabledWarmTransferCoordinatorTest, IdleStateBeforeStart) {
    EXPECT_EQ(coordinator.state(), CoordinatorState::Idle);
}

TEST_F(DisabledWarmTransferCoordinatorTest, DisabledEslFailsImmediatelyInStart) {
    bool        fired = false;
    bool        success = true;
    std::string detail;

    TransferCoordinatorCallbacks cbs;
    cbs.on_transfer_completed = [&](bool s, std::string /*dest*/, std::string d) {
        fired = true; success = s; detail = std::move(d);
    };

    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "caller_requested_human", "tid-1", "+15550001111", "announcement_moh"},
        std::move(cbs));

    EXPECT_TRUE(fired);
    EXPECT_FALSE(success);
    EXPECT_EQ(detail, "esl_disabled");
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);
}

TEST_F(DisabledWarmTransferCoordinatorTest, ShutdownBeforeStartIsANoOp) {
    coordinator.shutdown();
    EXPECT_EQ(coordinator.state(), CoordinatorState::Idle);
}

TEST_F(DisabledWarmTransferCoordinatorTest, ShutdownAfterCompletionIsIdempotent) {
    TransferCoordinatorCallbacks cbs;
    cbs.on_transfer_completed = [](bool, std::string, std::string) {};
    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "x", "tid-2", "+15550001111", "announcement_moh"}, std::move(cbs));

    ASSERT_EQ(coordinator.state(), CoordinatorState::Completed);
    coordinator.shutdown();
    coordinator.shutdown();
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);
}

TEST_F(DisabledWarmTransferCoordinatorTest, CancelBeforeStartIsANoOp) {
    coordinator.cancel();
    EXPECT_EQ(coordinator.state(), CoordinatorState::Idle);
}

TEST_F(DisabledWarmTransferCoordinatorTest, CancelAfterCompletionIsANoOp) {
    TransferCoordinatorCallbacks cbs;
    cbs.on_transfer_completed = [](bool, std::string, std::string) {};
    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "x", "tid-3", "+15550001111", "announcement_moh"}, std::move(cbs));

    coordinator.cancel();  // state is Completed, not Active — no-op, not a crash
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);
}

TEST_F(DisabledWarmTransferCoordinatorTest, MissingCallbackDoesNotCrash) {
    TransferCoordinatorCallbacks cbs;  // on_transfer_completed left unset
    coordinator.start(
        TransferCoordinatorContext{"call-uuid-1", "1001", "x", "tid-4", "+15550001111", "announcement_moh"}, std::move(cbs));
    SUCCEED();
}

// ── Caller ID / waiting experience ──────────────────────────────────────────

TEST(WarmTransferCoordinatorLiveTest, AnnouncementSilenceSkipsHold) {
    MultiCommandFakeEslServer server;
    // No uuid_hold at all — originate is the FIRST command issued.
    server.scripted_replies = {
        bgapi_reply_frame("job-uuid-silence"),   // bgapi originate
        api_response_frame("+OK"),                // uuid_hold off (still called unconditionally)
        api_response_frame("+OK"),                // uuid_audio_fork stop
        api_response_frame("+OK"),                // uuid_bridge
    };
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};
    TransferCorrelator leg_correlator;
    TransferCorrelator job_correlator;
    ObservabilityContext obs{"session-1", "tenant-1", "trace-1", "call-uuid-1", ""};
    ContextLogger log{obs, logger};
    WarmTransferCoordinator coordinator{client, leg_correlator, job_correlator, log};

    TransferCoordinatorCallbacks cbs;
    bool success = false;
    cbs.on_transfer_completed = [&](bool s, std::string, std::string) { success = s; };

    coordinator.start(
        TransferCoordinatorContext{"customer-uuid", "1001", "x", "tid-9",
                                   "+15550002222", "announcement_silence"},
        std::move(cbs));
    ASSERT_TRUE(job_correlator.resolve("job-uuid-silence", true, "agent-uuid-silence"));

    EXPECT_TRUE(success);
    ASSERT_EQ(server.received_commands.size(), 4u);
    EXPECT_EQ(server.received_commands[0],
              "bgapi originate {origination_caller_id_number=+15550002222}"
              "sofia/external/sip:1001@127.0.0.1:5060 &park()");
    EXPECT_EQ(server.received_commands[1], "api uuid_hold off customer-uuid");
}

TEST(WarmTransferCoordinatorLiveTest, UnrecognizedWaitingExperienceDefaultsToHold) {
    // Empty/unknown value must behave like the "announcement_moh" default.
    MultiCommandFakeEslServer server;
    server.scripted_replies = {api_response_frame("+OK")};  // uuid_hold only
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};
    TransferCorrelator leg_correlator;
    TransferCorrelator job_correlator;
    ObservabilityContext obs{"session-1", "tenant-1", "trace-1", "call-uuid-1", ""};
    ContextLogger log{obs, logger};
    WarmTransferCoordinator coordinator{client, leg_correlator, job_correlator, log};

    TransferCoordinatorCallbacks cbs;
    coordinator.start(
        TransferCoordinatorContext{"customer-uuid", "1001", "x", "tid-10", "+15550003333", ""},
        std::move(cbs));

    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_hold customer-uuid");

    coordinator.shutdown();
}

// ── Live sequencing against a scripted multi-command ESL connection ────────

TEST(WarmTransferCoordinatorLiveTest, SuccessfulAnswerBridgesAndReportsSuccess) {
    MultiCommandFakeEslServer server;
    server.scripted_replies = {
        api_response_frame("+OK"),               // uuid_hold
        bgapi_reply_frame("job-uuid-1"),          // bgapi originate
        api_response_frame("+OK"),                // uuid_hold off
        api_response_frame("+OK"),                // uuid_audio_fork stop
        api_response_frame("+OK"),                // uuid_bridge
    };
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};
    TransferCorrelator leg_correlator;
    TransferCorrelator job_correlator;
    ObservabilityContext obs{"session-1", "tenant-1", "trace-1", "call-uuid-1", ""};
    ContextLogger log{obs, logger};
    WarmTransferCoordinator coordinator{client, leg_correlator, job_correlator, log};

    bool        fired = false;
    bool        success = false;
    std::string detail;
    bool        handoff_fired = false;
    bool        handoff_fired_before_completion = false;
    TransferCoordinatorCallbacks cbs;
    cbs.on_media_handoff = [&] {
        handoff_fired = true;
        handoff_fired_before_completion = !fired;  // must fire strictly before completion
    };
    cbs.on_transfer_completed = [&](bool s, std::string /*dest*/, std::string d) {
        fired = true; success = s; detail = std::move(d);
    };

    coordinator.start(
        TransferCoordinatorContext{"customer-uuid", "1001", "caller_requested_human", "tid-5", "+15550001111", "announcement_moh"},
        std::move(cbs));

    ASSERT_EQ(coordinator.state(), CoordinatorState::Active);
    EXPECT_FALSE(fired);  // still waiting on BACKGROUND_JOB
    EXPECT_FALSE(handoff_fired);  // not yet — agent hasn't answered

    // BACKGROUND_JOB result text is the agent leg's channel uuid.
    ASSERT_TRUE(job_correlator.resolve("job-uuid-1", true, "agent-uuid-1"));

    EXPECT_TRUE(fired);
    EXPECT_TRUE(success);
    EXPECT_EQ(detail, "bridged");
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);
    // Must fire before stop_audio_fork's WebSocket disconnect can arrive.
    EXPECT_TRUE(handoff_fired);
    EXPECT_TRUE(handoff_fired_before_completion);

    ASSERT_EQ(server.received_commands.size(), 5u);
    EXPECT_EQ(server.received_commands[0], "api uuid_hold customer-uuid");
    EXPECT_EQ(server.received_commands[1],
              "bgapi originate {origination_caller_id_number=+15550001111}"
              "sofia/external/sip:1001@127.0.0.1:5060 &park()");
    EXPECT_EQ(server.received_commands[2], "api uuid_hold off customer-uuid");
    EXPECT_EQ(server.received_commands[3], "api uuid_audio_fork customer-uuid stop");
    EXPECT_EQ(server.received_commands[4], "api uuid_bridge customer-uuid agent-uuid-1");
}

TEST(WarmTransferCoordinatorLiveTest, AgentNoAnswerReportsFailureWithoutBridging) {
    MultiCommandFakeEslServer server;
    server.scripted_replies = {
        api_response_frame("+OK"),        // uuid_hold
        bgapi_reply_frame("job-uuid-2"),  // bgapi originate
        api_response_frame("+OK"),        // uuid_hold off — caller must not be left on MOH
    };
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};
    TransferCorrelator leg_correlator;
    TransferCorrelator job_correlator;
    ObservabilityContext obs{"session-1", "tenant-1", "trace-1", "call-uuid-1", ""};
    ContextLogger log{obs, logger};
    WarmTransferCoordinator coordinator{client, leg_correlator, job_correlator, log};

    bool        fired = false;
    bool        success = true;
    std::string detail;
    bool        handoff_fired = false;
    TransferCoordinatorCallbacks cbs;
    cbs.on_media_handoff = [&] { handoff_fired = true; };
    cbs.on_transfer_completed = [&](bool s, std::string /*dest*/, std::string d) {
        fired = true; success = s; detail = std::move(d);
    };

    coordinator.start(
        TransferCoordinatorContext{"customer-uuid", "1001", "x", "tid-6", "+15550001111", "announcement_moh"}, std::move(cbs));

    ASSERT_TRUE(job_correlator.resolve("job-uuid-2", false, "NO_ANSWER"));

    EXPECT_TRUE(fired);
    EXPECT_FALSE(success);
    EXPECT_EQ(detail, "NO_ANSWER");
    // Never handed off, so CallSession's own hangup must not be suppressed.
    EXPECT_FALSE(handoff_fired);
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);
    // No bridge, but the caller must still be taken off hold.
    ASSERT_EQ(server.received_commands.size(), 3u);
    EXPECT_EQ(server.received_commands[2], "api uuid_hold off customer-uuid");
}

TEST(WarmTransferCoordinatorLiveTest, BridgeFailureHangsUpAgentAndReportsFailure) {
    MultiCommandFakeEslServer server;
    server.scripted_replies = {
        api_response_frame("+OK"),               // uuid_hold
        bgapi_reply_frame("job-uuid-3"),          // bgapi originate
        api_response_frame("+OK"),                // uuid_hold off
        api_response_frame("+OK"),                // uuid_audio_fork stop
        api_response_frame("-ERR NO_ANSWER"),     // uuid_bridge fails
        api_response_frame("+OK"),                // uuid_kill (hangup agent)
    };
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};
    TransferCorrelator leg_correlator;
    TransferCorrelator job_correlator;
    ObservabilityContext obs{"session-1", "tenant-1", "trace-1", "call-uuid-1", ""};
    ContextLogger log{obs, logger};
    WarmTransferCoordinator coordinator{client, leg_correlator, job_correlator, log};

    bool        fired = false;
    bool        success = true;
    std::string detail;
    TransferCoordinatorCallbacks cbs;
    cbs.on_transfer_completed = [&](bool s, std::string /*dest*/, std::string d) {
        fired = true; success = s; detail = std::move(d);
    };

    coordinator.start(
        TransferCoordinatorContext{"customer-uuid", "1001", "x", "tid-7", "+15550001111", "announcement_moh"}, std::move(cbs));
    ASSERT_TRUE(job_correlator.resolve("job-uuid-3", true, "agent-uuid-3"));

    EXPECT_TRUE(fired);
    EXPECT_FALSE(success);
    EXPECT_EQ(detail, "bridge_failed:-ERR NO_ANSWER");
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);

    ASSERT_EQ(server.received_commands.size(), 6u);
    EXPECT_EQ(server.received_commands[4], "api uuid_bridge customer-uuid agent-uuid-3");
    EXPECT_EQ(server.received_commands[5], "api uuid_kill agent-uuid-3");
}

TEST(WarmTransferCoordinatorLiveTest, CancelBeforeAgentAnswersUnholdsAndReportsFailure) {
    MultiCommandFakeEslServer server;
    server.scripted_replies = {
        api_response_frame("+OK"),        // uuid_hold (start)
        bgapi_reply_frame("job-uuid-4"),  // bgapi originate
        api_response_frame("+OK"),        // uuid_hold off (cancel's unhold)
    };
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};
    TransferCorrelator leg_correlator;
    TransferCorrelator job_correlator;
    ObservabilityContext obs{"session-1", "tenant-1", "trace-1", "call-uuid-1", ""};
    ContextLogger log{obs, logger};
    WarmTransferCoordinator coordinator{client, leg_correlator, job_correlator, log};

    bool        fired = false;
    bool        success = true;
    std::string detail;
    TransferCoordinatorCallbacks cbs;
    cbs.on_transfer_completed = [&](bool s, std::string /*dest*/, std::string d) {
        fired = true; success = s; detail = std::move(d);
    };

    coordinator.start(
        TransferCoordinatorContext{"customer-uuid", "1001", "x", "tid-8", "+15550001111", "announcement_moh"}, std::move(cbs));
    ASSERT_EQ(coordinator.state(), CoordinatorState::Active);

    coordinator.cancel();  // agent leg not yet known — nothing to hang up

    EXPECT_TRUE(fired);
    EXPECT_FALSE(success);
    EXPECT_EQ(detail, "cancelled");
    EXPECT_EQ(coordinator.state(), CoordinatorState::Completed);

    // cancel() removed the watch, so a late BACKGROUND_JOB resolves nothing.
    EXPECT_FALSE(job_correlator.resolve("job-uuid-4", true, "agent-uuid-4"));
}

} // namespace
