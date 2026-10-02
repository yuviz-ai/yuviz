// EslClient command tests against a minimal real ESL server on loopback
// (auth, api command/response framing); asserts exact commands and reply handling.

#include <gtest/gtest.h>

#include "config/Config.h"
#include "logging/Logger.h"
#include "telephony/EslClient.h"
#include "telephony/TransferRequest.h"

#include <arpa/inet.h>
#include <atomic>
#include <netinet/in.h>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

using namespace voiceai;
using namespace std::chrono_literals;

// Reads until a blank-line ("\n\n") terminator; returns everything before it.
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

// One scripted exchange: after auth (unless `reject_auth`), one command and a framed reply.
struct FakeEslServer {
    int listen_fd{-1};
    uint16_t port{0};
    std::thread server_thread;
    std::atomic<bool> stop{false};

    std::string expected_password = "ClueCon";
    bool        reject_auth        = false;
    bool        close_before_reply = false;  // simulate ESL unreachable mid-command
    std::string next_reply_body    = "+OK";  // body of the api/response to the next command

    std::vector<std::string> received_commands;  // every "api ..." command seen, in order

    void start() {
        listen_fd = ::socket(AF_INET, SOCK_STREAM, 0);
        int opt = 1;
        ::setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

        sockaddr_in addr{};
        addr.sin_family      = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port        = 0;  // ephemeral
        ::bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));

        sockaddr_in bound{};
        socklen_t   len = sizeof(bound);
        ::getsockname(listen_fd, reinterpret_cast<sockaddr*>(&bound), &len);
        port = ntohs(bound.sin_port);

        ::listen(listen_fd, 4);

        server_thread = std::thread([this] { run(); });
    }

    void run() {
        while (!stop.load()) {
            sockaddr_in peer{};
            socklen_t   plen = sizeof(peer);
            const int   fd   = ::accept(listen_fd, reinterpret_cast<sockaddr*>(&peer), &plen);
            if (fd < 0) return;  // listen_fd closed → stop()

            send_all(fd, "Content-Type: auth/request\n\n");
            const std::string auth_line = recv_until_blank_line(fd);

            const bool ok = !reject_auth && auth_line == ("auth " + expected_password);
            send_all(fd, ok
                ? "Content-Type: command/reply\nReply-Text: +OK accepted\n\n"
                : "Content-Type: command/reply\nReply-Text: -ERR invalid\n\n");
            if (!ok) { ::close(fd); continue; }

            // One command per connection is all this test needs.
            const std::string command = recv_until_blank_line(fd);
            received_commands.push_back(command);

            if (close_before_reply) { ::close(fd); continue; }

            const std::string body = "Content-Type: api/response\nContent-Length: "
                + std::to_string(next_reply_body.size()) + "\n\n" + next_reply_body;
            send_all(fd, body);
            ::close(fd);
        }
    }

    void stop_server() {
        stop.store(true);
        if (listen_fd >= 0) ::shutdown(listen_fd, SHUT_RDWR);
        if (listen_fd >= 0) ::close(listen_fd);
        if (server_thread.joinable()) server_thread.join();
    }

    ~FakeEslServer() { stop_server(); }
};

TransferRequest make_req(std::string call_id, std::string destination, std::string reason = "x") {
    return TransferRequest{std::move(call_id), "cold", std::move(destination), std::move(reason)};
}

EslConfig make_cfg(uint16_t port) {
    EslConfig cfg;
    cfg.enabled            = true;
    cfg.host               = "127.0.0.1";
    cfg.port               = port;
    cfg.password           = "ClueCon";
    cfg.connect_timeout_ms = 500;
    cfg.sip_proxy_host     = "192.168.0.116";  // Kamailio; numbers are dialed through it
    return cfg;
}

} // namespace

// ── uuid_transfer command construction ──────────────────────────────────────

TEST(EslClientTransferTest, PhoneNumberDestinationBridgesThroughTheSipProxy) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "+OK";

    Logger logger = Logger::make_null();
    EslConfig cfg = make_cfg(server.port);
    cfg.sip_proxy_host = "192.168.0.116";
    cfg.sip_proxy_port = 5060;
    EslClient client{cfg, logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-1", "1005", "customer_requested"), error);

    EXPECT_TRUE(ok);
    EXPECT_TRUE(error.empty());
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0],
              "api uuid_transfer call-uuid-1 'bridge:sofia/external/sip:1005@192.168.0.116:5060' inline");
}

// The stock "default" context maps 779 to eavesdrop-all and 886 to intercept:
// a tenant-chosen number must never be looked up in any dialplan context.
TEST(EslClientTransferTest, NoTransferDestinationReachesADialplanContext) {
    for (const char* dest : {"779", "886", "15005", "+919876543210", "sip:agent@example.com"}) {
        FakeEslServer server;
        server.start();
        server.next_reply_body = "+OK";
        Logger logger = Logger::make_null();
        EslClient client{make_cfg(server.port), logger};
        std::string error;
        ASSERT_TRUE(client.transfer(make_req("call-uuid-9", dest), error)) << dest;
        ASSERT_EQ(server.received_commands.size(), 1u) << dest;
        const std::string& cmd = server.received_commands[0];
        EXPECT_EQ(cmd.find(" XML "), std::string::npos) << cmd;
        EXPECT_EQ(cmd.rfind("api uuid_transfer call-uuid-9 'bridge:sofia/external/", 0), 0u) << cmd;
        EXPECT_EQ(cmd.substr(cmd.size() - 8), "' inline") << cmd;
    }
}

// The platform's own AI numbers (00_voice_ai.xml) bridged from FreeSWITCH hit
// Kamailio's loop guard: refused up front so the caller is never left in
// silence waiting on a transfer that cannot connect.
TEST(EslClientTransferTest, PlatformAiNumberRefusedBeforeAnyCommand) {
    for (const char* dest : {"788", "5000", "5005", "5009"}) {
        FakeEslServer server;
        server.start();
        Logger logger = Logger::make_null();
        EslClient client{make_cfg(server.port), logger};

        std::string error;
        EXPECT_FALSE(client.transfer(make_req("call-uuid-p", dest), error)) << dest;
        EXPECT_EQ(error, "platform_destination") << dest;
        std::string job_uuid;
        EXPECT_FALSE(client.originate_async(dest, "+15551234567", job_uuid, error)) << dest;
        EXPECT_EQ(error, "platform_destination") << dest;
        EXPECT_TRUE(server.received_commands.empty()) << dest;
    }
}

// A SIP URI at the switch itself (or the proxy in front of it) enters one of
// FreeSWITCH's own dialplan contexts — stock "public" hands 10xx and the
// 35xx conference rooms on to "default".
TEST(EslClientTransferTest, SipUriAtThePlatformRefusedBeforeAnyCommand) {
    const std::vector<std::string> platform_uris = {
        "sip:3500@127.0.0.1:5080",
        "sips:3500@127.0.0.1",
        "sip:3500@127.1:5080",              // inet_aton shorthand
        "sip:779@2130706433",               // 127.0.0.1 as one integer
        "sip:3500@0.0.0.0",
        "sip:3500@localhost:5080",
        "sip:3500@LocalHost.",
        "sip:3500@fs.localhost",
        "sip:3500@192.168.0.116:5080",      // esl.sip_proxy_host
        "sip:3500@pbx.example.com;maddr=127.0.0.1",
        "sip:3500@pbx.example.com;MADDR=10.0.0.1",
    };
    for (const auto& dest : platform_uris) {
        FakeEslServer server;
        server.start();
        Logger logger = Logger::make_null();
        EslClient client{make_cfg(server.port), logger};

        std::string error;
        EXPECT_FALSE(client.transfer(make_req("call-uuid-s", dest), error)) << dest;
        EXPECT_EQ(error, "platform_destination") << dest;
        std::string job_uuid;
        EXPECT_FALSE(client.originate_async(dest, "+15551234567", job_uuid, error)) << dest;
        EXPECT_EQ(error, "platform_destination") << dest;
        EXPECT_TRUE(server.received_commands.empty()) << dest;
    }
}

TEST(EslClientTransferTest, SipUriAtTheEslHostRefused) {
    Logger logger = Logger::make_null();
    EslConfig cfg = make_cfg(1);  // never connected: refused before any I/O
    cfg.host = "10.20.30.40";
    EslClient client{cfg, logger};

    std::string error;
    EXPECT_FALSE(client.transfer(make_req("call-uuid-e", "sip:3500@10.20.30.40:5080"), error));
    EXPECT_EQ(error, "platform_destination");
}

TEST(EslClientTransferTest, SipUriElsewhereStillAllowedWhenHostOnlyLooksLocal) {
    for (const char* dest : {"sip:agent@127.example.com", "sip:localhost@pbx.example.com",
                             "sip:agent@192.168.0.117:5060"}) {
        FakeEslServer server;
        server.start();
        server.next_reply_body = "+OK";
        Logger logger = Logger::make_null();
        EslClient client{make_cfg(server.port), logger};

        std::string error;
        EXPECT_TRUE(client.transfer(make_req("call-uuid-o", dest), error)) << dest << " " << error;
        EXPECT_EQ(server.received_commands.size(), 1u) << dest;
    }
}

// Without a proxy host, refuse numbers rather than INVITE a wrong host (~32 s of silence).
// A SIP URI does not need the proxy.
TEST(EslClientTransferTest, NumberRefusedWhenSipProxyHostUnset) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "+OK";
    Logger logger = Logger::make_null();
    EslConfig cfg = make_cfg(server.port);
    cfg.sip_proxy_host.clear();
    EslClient client{cfg, logger};

    std::string error;
    EXPECT_FALSE(client.transfer(make_req("call-uuid-u", "1005"), error));
    EXPECT_EQ(error, "sip_proxy_host_unset");
    std::string job_uuid;
    EXPECT_FALSE(client.originate_async("1005", "+15551234567", job_uuid, error));
    EXPECT_EQ(error, "sip_proxy_host_unset");
    EXPECT_TRUE(server.received_commands.empty());

    EXPECT_TRUE(client.transfer(make_req("call-uuid-u", "sip:agent@example.com"), error));
    EXPECT_EQ(server.received_commands.size(), 1u);
}

TEST(EslClientTransferTest, SipProxyHostDefaultsToUnset) {
    EXPECT_TRUE(EslConfig{}.sip_proxy_host.empty());
}

TEST(EslClientTransferTest, SipUriDestinationUsesInlineBridge) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "+OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-2", "sip:agent@example.com",
                                                  "escalation_threshold_exceeded"), error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0],
              "api uuid_transfer call-uuid-2 'bridge:sofia/external/sip:agent@example.com' inline");
}

TEST(EslClientTransferTest, SipsUriAlsoDetectedAsSipUri) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "+OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    client.transfer(make_req("call-uuid-3", "sips:agent@example.com"), error);

    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_NE(server.received_commands[0].find("bridge:sofia/external/sips:agent@example.com"),
              std::string::npos);
}

// ── Reply interpretation ─────────────────────────────────────────────────────

TEST(EslClientTransferTest, RejectedTransferReturnsFalseWithReplyAsError) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "-ERR NO_ROUTE_DESTINATION";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-4", "9999999"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "-ERR NO_ROUTE_DESTINATION");
}

// ── Guard clauses (no network round trip expected) ──────────────────────────

TEST(EslClientTransferTest, DisabledConfigReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);  // port 1: nothing listens there
    cfg.enabled = false;
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-5", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_disabled");
}

TEST(EslClientTransferTest, EmptyUuidReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    const bool ok = client.transfer(make_req("", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "empty_uuid");
}

TEST(EslClientTransferTest, EmptyDestinationReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-6", ""), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "empty_destination");
}

// ── Tenant-configured values pasted into ESL commands ───────────────────────

const std::vector<std::string> kInjectedDestinations = {
    "1001\n\napi hupall",
    "1001\r\n\r\napi hupall",
    "1001 XML public",
    "sip:agent@example.com' inline\n\napi hupall",
    "sip:agent@example.com\n\napi hupall",
    "{sip_h_X-Yuviz-Leg=transfer}sip:1002@example.com",
    "sip:a@b,sofia/external/sip:c@d",
    "sip:${global_getvar(x)}@example.com",
    "1",
    "1234567890123456",
};

TEST(EslClientTransferTest, InjectedDestinationRefusedBeforeAnyCommand) {
    for (const auto& dest : kInjectedDestinations) {
        FakeEslServer server;
        server.start();
        Logger logger = Logger::make_null();
        EslClient client{make_cfg(server.port), logger};

        std::string error;
        EXPECT_FALSE(client.transfer(make_req("call-uuid-x", dest), error)) << dest;
        EXPECT_EQ(error, "invalid_destination") << dest;
        EXPECT_TRUE(server.received_commands.empty()) << dest;
    }
}

TEST(EslClientTransferTest, SipUriWithTransportParamStillAllowed) {
    FakeEslServer server;
    server.start();
    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    EXPECT_TRUE(client.transfer(make_req("call-uuid-y", "sip:agent@pbx.example.com:5070;transport=tcp"), error));
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0],
              "api uuid_transfer call-uuid-y 'bridge:sofia/external/sip:agent@pbx.example.com:5070;transport=tcp' inline");
}

TEST(EslClientOriginateAsyncTest, InjectedDestinationRefusedBeforeAnyCommand) {
    for (const auto& dest : kInjectedDestinations) {
        FakeEslServer server;
        server.start();
        Logger logger = Logger::make_null();
        EslClient client{make_cfg(server.port), logger};

        std::string job_uuid, error;
        EXPECT_FALSE(client.originate_async(dest, "+15551234567", job_uuid, error)) << dest;
        EXPECT_EQ(error, "invalid_destination") << dest;
        EXPECT_TRUE(server.received_commands.empty()) << dest;
    }
}

TEST(EslClientOriginateAsyncTest, InjectedCallerIdIsDroppedNotInterpolated) {
    const std::vector<std::string> bad_caller_ids = {
        "+15551234567}\n\napi hupall\n\n",
        "+1555,sip_h_X-Yuviz-Leg=transfer",
        "anonymous",
    };
    for (const auto& caller_id : bad_caller_ids) {
        FakeEslServer server;
        server.start();
        server.next_reply_body = "Reply-Text: +OK Job-UUID: job-1\nJob-UUID: job-1";
        Logger logger = Logger::make_null();
        EslConfig cfg = make_cfg(server.port);
        cfg.sip_proxy_host = "192.168.0.116";
        cfg.sip_proxy_port = 5060;
        EslClient client{cfg, logger};

        std::string job_uuid, error;
        EXPECT_TRUE(client.originate_async("1001", caller_id, job_uuid, error)) << caller_id;
        ASSERT_EQ(server.received_commands.size(), 1u) << caller_id;
        EXPECT_EQ(server.received_commands[0],
                  "bgapi originate sofia/external/sip:1001@192.168.0.116:5060 &park()") << caller_id;
    }
}

const std::vector<std::string> kInjectedUuids = {
    "call-uuid\n\napi hupall",
    "call-uuid api hupall",
    "call-uuid{x=1}",
    std::string(65, 'a'),
};

TEST(EslClientUuidGuardTest, EveryUuidCommandRefusesNonUuidInput) {
    for (const auto& bad : kInjectedUuids) {
        FakeEslServer server;
        server.start();
        server.next_reply_body = "Reply-Text: +OK";
        Logger logger = Logger::make_null();
        EslClient client{make_cfg(server.port), logger};
        std::string error;

        EXPECT_FALSE(client.transfer(make_req(bad, "1005"), error));
        EXPECT_EQ(error, "invalid_uuid");
        EXPECT_FALSE(client.bridge(bad, "agent-uuid", error));
        EXPECT_EQ(error, "invalid_uuid");
        EXPECT_FALSE(client.bridge("customer-uuid", bad, error));
        EXPECT_EQ(error, "invalid_uuid");
        EXPECT_FALSE(client.stop_audio_fork(bad, error));
        EXPECT_EQ(error, "invalid_uuid");
        EXPECT_FALSE(client.hold(bad, error));
        EXPECT_EQ(error, "invalid_uuid");
        EXPECT_FALSE(client.unhold(bad, error));
        EXPECT_EQ(error, "invalid_uuid");
        client.hangup(bad, "caller_hangup");

        EXPECT_TRUE(server.received_commands.empty()) << bad;
    }
}

// ── Connection failure ───────────────────────────────────────────────────────

TEST(EslClientTransferTest, UnreachableEslReturnsFalse) {
    // Nothing listens on this port; connect() fails fast.
    EslConfig cfg = make_cfg(1);
    cfg.connect_timeout_ms = 100;
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-7", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_unreachable");
}

TEST(EslClientTransferTest, AuthRejectedReturnsFalse) {
    FakeEslServer server;
    server.reject_auth = true;
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-8", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_unreachable");
    EXPECT_TRUE(server.received_commands.empty());  // never got past auth
}

TEST(EslClientTransferTest, ConnectionDroppedMidCommandReturnsFalse) {
    FakeEslServer server;
    server.close_before_reply = true;
    server.start();

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.transfer(make_req("call-uuid-9", "1005"), error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_unreachable");
    ASSERT_EQ(server.received_commands.size(), 1u);  // command was sent before the drop
}

// ── hangup() ────────────────────────────────────────────────────────────────

TEST(EslClientHangupTest, SuccessfulHangupIssuesUuidKill) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "+OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    client.hangup("call-uuid-10", "caller_hangup");

    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_kill call-uuid-10");
}

TEST(EslClientHangupTest, EmptyUuidSkipsConnectionEntirely) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    // Must not throw and must not hang waiting on a nonexistent server.
    client.hangup("", "caller_hangup");
}

// ── originate_async() / bridge() / stop_audio_fork() / hold() / unhold() ───
// bgapi replies carry Job-UUID as a header (no body), so next_reply_body here is header text.

TEST(EslClientOriginateAsyncTest, PlainExtensionDialsThroughSipProxyDirectly) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK Job-UUID: job-abc-123\nJob-UUID: job-abc-123";

    Logger logger = Logger::make_null();
    EslConfig cfg = make_cfg(server.port);
    cfg.sip_proxy_host = "192.168.0.116";
    cfg.sip_proxy_port = 5060;
    EslClient client{cfg, logger};

    std::string job_uuid, error;
    const bool ok = client.originate_async("1001", "+15551234567", job_uuid, error);

    EXPECT_TRUE(ok);
    EXPECT_EQ(job_uuid, "job-abc-123");
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0],
              "bgapi originate {origination_caller_id_number=+15551234567}"
              "sofia/external/sip:1001@192.168.0.116:5060 &park()");
}

TEST(EslClientOriginateAsyncTest, SipUriDestinationDialsDirectly) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK Job-UUID: job-xyz\nJob-UUID: job-xyz";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string job_uuid, error;
    client.originate_async("sip:agent@example.com", "+15551234567", job_uuid, error);

    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0],
              "bgapi originate {origination_caller_id_number=+15551234567}"
              "sofia/external/sip:agent@example.com &park()");
}

TEST(EslClientOriginateAsyncTest, RejectedOriginateReturnsFalseWithReplyAsError) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: -ERR DESTINATION_OUT_OF_ORDER";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string job_uuid, error;
    const bool ok = client.originate_async("1001", "+15551234567", job_uuid, error);

    EXPECT_FALSE(ok);
    EXPECT_TRUE(job_uuid.empty());
    EXPECT_EQ(error, "Reply-Text: -ERR DESTINATION_OUT_OF_ORDER");
}

TEST(EslClientOriginateAsyncTest, DisabledConfigReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    cfg.enabled = false;
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string job_uuid, error;
    const bool ok = client.originate_async("1001", "+15551234567", job_uuid, error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "esl_disabled");
}

TEST(EslClientOriginateAsyncTest, EmptyDestinationReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string job_uuid, error;
    const bool ok = client.originate_async("", "+15551234567", job_uuid, error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "empty_destination");
}

TEST(EslClientBridgeTest, SuccessfulBridgeIssuesUuidBridge) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.bridge("customer-uuid", "agent-uuid", error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_bridge customer-uuid agent-uuid");
}

TEST(EslClientBridgeTest, RejectedBridgeReturnsFalse) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: -ERR NO_ANSWER";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.bridge("customer-uuid", "agent-uuid", error);

    EXPECT_FALSE(ok);
    EXPECT_EQ(error, "Reply-Text: -ERR NO_ANSWER");
}

TEST(EslClientBridgeTest, EmptyUuidReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    EXPECT_FALSE(client.bridge("", "agent-uuid", error));
    EXPECT_EQ(error, "empty_uuid");
    EXPECT_FALSE(client.bridge("customer-uuid", "", error));
    EXPECT_EQ(error, "empty_uuid");
}

TEST(EslClientStopAudioForkTest, SuccessfulStopIssuesUuidAudioForkStop) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.stop_audio_fork("customer-uuid", error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_audio_fork customer-uuid stop");
}

TEST(EslClientStopAudioForkTest, EmptyUuidReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    EXPECT_FALSE(client.stop_audio_fork("", error));
    EXPECT_EQ(error, "empty_uuid");
}

TEST(EslClientHoldTest, SuccessfulHoldIssuesUuidHold) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.hold("customer-uuid", error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_hold customer-uuid");
}

TEST(EslClientHoldTest, RejectedHoldReturnsFalse) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: -ERR NO_SUCH_CHANNEL";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    EXPECT_FALSE(client.hold("customer-uuid", error));
    EXPECT_EQ(error, "Reply-Text: -ERR NO_SUCH_CHANNEL");
}

TEST(EslClientUnholdTest, SuccessfulUnholdIssuesUuidHoldOff) {
    FakeEslServer server;
    server.start();
    server.next_reply_body = "Reply-Text: +OK";

    Logger logger = Logger::make_null();
    EslClient client{make_cfg(server.port), logger};

    std::string error;
    const bool ok = client.unhold("customer-uuid", error);

    EXPECT_TRUE(ok);
    ASSERT_EQ(server.received_commands.size(), 1u);
    EXPECT_EQ(server.received_commands[0], "api uuid_hold off customer-uuid");
}

TEST(EslClientUnholdTest, EmptyUuidReturnsFalseWithoutConnecting) {
    EslConfig cfg = make_cfg(1);
    Logger logger = Logger::make_null();
    EslClient client{cfg, logger};

    std::string error;
    EXPECT_FALSE(client.unhold("", error));
    EXPECT_EQ(error, "empty_uuid");
}
