#include <gtest/gtest.h>
#include "config/Config.h"
#include "config/RedisClient.h"
#include "logging/Logger.h"
#include "telephony/EslClient.h"
#include "telephony/TransferRequest.h"
#include <cstdlib>
#include <fstream>
#include <filesystem>
#include <string>

namespace {

class ConfigTest : public ::testing::Test {
protected:
    std::filesystem::path tmp_yaml_;

    void SetUp() override {
        tmp_yaml_ = std::filesystem::temp_directory_path() / "test_gateway.yaml";
    }

    void TearDown() override {
        std::filesystem::remove(tmp_yaml_);
    }

    void write_yaml(const std::string& content) {
        std::ofstream f{tmp_yaml_};
        f << content;
    }
};

TEST_F(ConfigTest, DefaultsAreAppliedWhenKeysMissing) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_EQ(cfg.websocket().port, 8080);
    EXPECT_EQ(cfg.websocket().host, "127.0.0.1");
    EXPECT_EQ(cfg.media().sample_rate, 16000u);
    EXPECT_EQ(cfg.media().channels, 1u);
}

TEST_F(ConfigTest, ValuesOverrideDefaults) {
    write_yaml(R"(
gateway:
  websocket:
    port: 9090
    host: "127.0.0.1"
    max_connections: 500
  media:
    sample_rate: 16000
    channels: 1
    frame_ms: 40
)");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_EQ(cfg.websocket().port, 9090);
    EXPECT_EQ(cfg.websocket().host, "127.0.0.1");
    EXPECT_EQ(cfg.websocket().max_connections, 500u);
    EXPECT_EQ(cfg.media().sample_rate, 16000u);
    EXPECT_EQ(cfg.media().frame_ms, 40u);
}

TEST_F(ConfigTest, ThrowsOnMissingFile) {
    voiceai::Config cfg;
    EXPECT_THROW(cfg.load("/nonexistent/path/config.yaml"), std::runtime_error);
}

// ── TenantConfig::from_default() ────────────────────────────────────────────

TEST_F(ConfigTest, FromDefaultCopiesAudioParams) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    const auto tc = voiceai::TenantConfig::from_default(cfg.gateway());

    EXPECT_EQ(tc.sample_rate, cfg.media().sample_rate);
    EXPECT_EQ(tc.channels,    cfg.media().channels);
    EXPECT_EQ(tc.frame_ms,    cfg.media().frame_ms);
}

TEST_F(ConfigTest, FromDefaultDerivesInboundFrameCapFromRingBufferMs) {
    // Default: ring_buffer_ms=500, frame_ms=20 → 500/20 = 25 frames
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    const auto tc = voiceai::TenantConfig::from_default(cfg.gateway());

    const size_t expected = cfg.media().ring_buffer_ms / cfg.media().frame_ms;
    EXPECT_EQ(tc.backpressure.max_inbound_queue_frames, expected);
}

TEST_F(ConfigTest, FromDefaultInboundFrameCapScalesWithFrameMs) {
    // frame_ms=40 → ring_buffer_ms(500)/40 = 12 frames
    write_yaml(R"(
gateway:
  media:
    frame_ms: 40
    ring_buffer_ms: 500
)");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    const auto tc = voiceai::TenantConfig::from_default(cfg.gateway());
    EXPECT_EQ(tc.backpressure.max_inbound_queue_frames, 500u / 40u);
}

TEST_F(ConfigTest, FromDefaultOutboundFrameCapCopiedFromPlaybackMaxFrames) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    const auto tc = voiceai::TenantConfig::from_default(cfg.gateway());
    EXPECT_EQ(tc.backpressure.max_outbound_queue_frames,
              cfg.media().playback_max_frames);
}

TEST_F(ConfigTest, ConversationSectionParsedFromYaml) {
    write_yaml(R"(
gateway:
  conversation:
    type: "grpc"
    endpoint: "10.0.0.1:50051"
    connect_timeout_ms: 3000
)");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_EQ(cfg.conversation().type,               "grpc");
    EXPECT_EQ(cfg.conversation().endpoint,           "10.0.0.1:50051");
    EXPECT_EQ(cfg.conversation().connect_timeout_ms, 3000u);
}

// ── Redis section ────────────────────────────────────────────────────────────

TEST_F(ConfigTest, RedisSectionDefaultsToDisabled) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_FALSE(cfg.gateway().redis.enabled);
    EXPECT_EQ(cfg.gateway().redis.port, 6379);
}

TEST_F(ConfigTest, RedisSectionParsedFromYaml) {
    write_yaml(R"(
gateway:
  redis:
    enabled: true
    host: "10.0.0.5"
    port: 6380
    connect_timeout_ms: 300
    command_timeout_ms: 150
)");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    EXPECT_TRUE(cfg.gateway().redis.enabled);
    EXPECT_EQ(cfg.gateway().redis.host, "10.0.0.5");
    EXPECT_EQ(cfg.gateway().redis.port, 6380);
    EXPECT_EQ(cfg.gateway().redis.connect_timeout_ms, 300u);
    EXPECT_EQ(cfg.gateway().redis.command_timeout_ms, 150u);
}

// ── TenantConfig::from_redis() ───────────────────────────────────────────────
// Hermetic: only the disabled-Redis path is tested; live Redis is verified manually.

TEST_F(ConfigTest, FromRedisFallsBackToDefaultsWhenRedisDisabled) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    voiceai::Logger logger = voiceai::Logger::make_null();
    voiceai::RedisClient redis{cfg.gateway().redis, logger};  // enabled=false by default

    const auto expected = voiceai::TenantConfig::from_default(cfg.gateway());
    const auto actual   = voiceai::TenantConfig::from_redis(redis, "default", cfg.gateway());

    EXPECT_EQ(actual.sample_rate,  expected.sample_rate);
    EXPECT_EQ(actual.vad_engine,   expected.vad_engine);
    EXPECT_EQ(actual.silero.hold_ms, expected.silero.hold_ms);
    EXPECT_EQ(actual.tenant_id, "default");
}

// ── resolve_route() ──────────────────────────────────────────────────────────

namespace {
using voiceai::LookupResult;
using voiceai::LookupStatus;
using voiceai::RoutingStatus;

const LookupResult kHit{LookupStatus::Hit,
                        R"({"tenant_slug": "acme", "agent_slug": "reception", "version": 24})"};
const LookupResult kMiss{LookupStatus::Miss, {}};
const LookupResult kError{LookupStatus::Error, {}};
}  // namespace

TEST_F(ConfigTest, RouteHitIsRoutedWithParsedSlugs) {
    voiceai::DidRouteCache cache;
    const auto r = voiceai::resolve_route("5002", kHit, cache);

    EXPECT_EQ(r.status, RoutingStatus::Routed);
    EXPECT_EQ(r.route.tenant_slug, "acme");
    EXPECT_EQ(r.route.agent_slug, "reception");
    EXPECT_EQ(r.route.version, 24u);
}

TEST_F(ConfigTest, RouteMissIsUnknownNotDefault) {
    voiceai::DidRouteCache cache;
    const auto r = voiceai::resolve_route("5002", kMiss, cache);

    EXPECT_EQ(r.status, RoutingStatus::Unknown);
    EXPECT_TRUE(r.route.tenant_slug.empty());
}

TEST_F(ConfigTest, RouteErrorWithoutCacheIsUnavailable) {
    voiceai::DidRouteCache cache;
    EXPECT_EQ(voiceai::resolve_route("5002", kError, cache).status, RoutingStatus::Unavailable);
}

TEST_F(ConfigTest, RouteErrorAfterHitServesLastKnownGood) {
    voiceai::DidRouteCache cache;
    (void)voiceai::resolve_route("5002", kHit, cache);
    const auto r = voiceai::resolve_route("5002", kError, cache);

    EXPECT_EQ(r.status, RoutingStatus::RoutedLkg);
    EXPECT_EQ(r.route.tenant_slug, "acme");
    EXPECT_EQ(r.route.agent_slug, "reception");
}

TEST_F(ConfigTest, RouteMissEvictsCachedRoute) {
    voiceai::DidRouteCache cache;
    (void)voiceai::resolve_route("5002", kHit, cache);
    (void)voiceai::resolve_route("5002", kMiss, cache);

    EXPECT_EQ(voiceai::resolve_route("5002", kError, cache).status, RoutingStatus::Unavailable);
}

TEST_F(ConfigTest, RouteCacheIsPerDid) {
    voiceai::DidRouteCache cache;
    (void)voiceai::resolve_route("5002", kHit, cache);
    EXPECT_EQ(voiceai::resolve_route("5003", kError, cache).status, RoutingStatus::Unavailable);
}

TEST_F(ConfigTest, RouteCorruptValueFallsBackLikeAnOutage) {
    voiceai::DidRouteCache cache;
    for (const char* bad : {"{not json", "[]", R"({"tenant_slug": "acme"})",
                            R"({"tenant_slug": "acme", "agent_slug": ""})",
                            R"({"tenant_slug": 1, "agent_slug": "x"})"}) {
        EXPECT_EQ(voiceai::resolve_route("5002", {LookupStatus::Hit, bad}, cache).status,
                  RoutingStatus::Unavailable) << bad;
    }
    (void)voiceai::resolve_route("5002", kHit, cache);
    EXPECT_EQ(voiceai::resolve_route("5002", {LookupStatus::Hit, "{bad"}, cache).status,
              RoutingStatus::RoutedLkg);
}

TEST_F(ConfigTest, RouteEmptyDidIsUnknownEvenFromCache) {
    voiceai::DidRouteCache cache;
    cache.put("", voiceai::PhoneRoute{"acme", "reception", 1});
    EXPECT_EQ(voiceai::resolve_route("", kHit, cache).status, RoutingStatus::Unknown);
    EXPECT_EQ(voiceai::resolve_route("", kError, cache).status, RoutingStatus::Unknown);
}

TEST_F(ConfigTest, RouteWithRedisDisabledIsUnavailable) {
    write_yaml("gateway:\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());

    voiceai::Logger logger = voiceai::Logger::make_null();
    voiceai::RedisClient redis{cfg.gateway().redis, logger};  // enabled=false by default
    voiceai::DidRouteCache cache;

    EXPECT_EQ(voiceai::resolve_route(redis, "5000", cache).status, RoutingStatus::Unavailable);
}

// ── CallMetadata::parse() ────────────────────────────────────────────────────

TEST_F(ConfigTest, CallMetadataParsesAllFields) {
    const auto md = voiceai::CallMetadata::parse(
        std::string(R"({"type":"start","call_id":"c1","did":"5000","ani":"5551234567","direction":"inbound"})"));

    EXPECT_EQ(md.did, "5000");
    EXPECT_EQ(md.ani, "5551234567");
    EXPECT_EQ(md.direction, "inbound");
}

TEST_F(ConfigTest, CallMetadataFallsBackOnMissingFrame) {
    const auto md = voiceai::CallMetadata::parse(std::nullopt);

    EXPECT_EQ(md.did, "");
    EXPECT_EQ(md.ani, "");
    EXPECT_EQ(md.direction, "inbound");
}

TEST_F(ConfigTest, CallMetadataFallsBackOnMalformedJson) {
    const auto md = voiceai::CallMetadata::parse(std::string("not json at all"));

    EXPECT_EQ(md.did, "");
    EXPECT_EQ(md.ani, "");
    EXPECT_EQ(md.direction, "inbound");
}

TEST_F(ConfigTest, CallMetadataDefaultsMissingFieldsIndividually) {
    const auto md = voiceai::CallMetadata::parse(std::string(R"({"did":"5001"})"));

    EXPECT_EQ(md.did, "5001");
    EXPECT_EQ(md.ani, "");           // not present — stays default
    EXPECT_EQ(md.direction, "inbound"); // not present — stays default
}

TEST_F(ConfigTest, CallMetadataIgnoresWrongTypedFields) {
    const auto md = voiceai::CallMetadata::parse(std::string(R"({"did":12345,"ani":"5551234567"})"));

    EXPECT_EQ(md.did, "");           // wrong type (number, not string) — stays default
    EXPECT_EQ(md.ani, "5551234567"); // correctly typed field still parses
}

} // namespace

// ── CallFsmTimerConfig transfer timeout ──────────────────────────────────────
// Asserts the compiled default and bound ordering; the Redis overlay needs live Redis.

TEST_F(ConfigTest, TransferTimeoutDefaultIs45sWithSaneBounds) {
    const voiceai::CallFsmTimerConfig t{};
    EXPECT_EQ(t.transfer_timeout, std::chrono::milliseconds{45'000});
    EXPECT_EQ(t.transfer_timeout, voiceai::CallFsmTimerConfig::transfer_timeout_default);
    EXPECT_LT(voiceai::CallFsmTimerConfig::transfer_timeout_min,
              voiceai::CallFsmTimerConfig::transfer_timeout_default);
    EXPECT_LT(voiceai::CallFsmTimerConfig::transfer_timeout_default,
              voiceai::CallFsmTimerConfig::transfer_timeout_max);
}

TEST_F(ConfigTest, NoSpeechTimeoutDefaultIs30sWithSaneBounds) {
    const voiceai::CallFsmTimerConfig t{};
    EXPECT_EQ(t.no_speech_timeout, std::chrono::milliseconds{30'000});
    EXPECT_EQ(t.no_speech_timeout, voiceai::CallFsmTimerConfig::no_speech_timeout_default);
    EXPECT_LT(voiceai::CallFsmTimerConfig::no_speech_timeout_min,
              voiceai::CallFsmTimerConfig::no_speech_timeout_default);
    EXPECT_LT(voiceai::CallFsmTimerConfig::no_speech_timeout_default,
              voiceai::CallFsmTimerConfig::no_speech_timeout_max);
}

// ── ESL settings from the environment (.env) ─────────────────────────────────
namespace {

class EslEnvConfigTest : public ConfigTest {
protected:
    void SetUp() override {
        ConfigTest::SetUp();
        for (const char* v : {"FREESWITCH_ESL_PASSWORD", "FREESWITCH_ESL_HOST", "FREESWITCH_ESL_PORT",
                              "SIP_PROXY_HOST", "SIP_PROXY_PORT", "CONVERSATION_SVC_TARGET", "REDIS_URL",
                              "GATEWAY_LISTEN_HOST"}) {
            ::unsetenv(v);
        }
    }
    void TearDown() override {
        SetUp();
        ConfigTest::TearDown();
    }
};

}  // namespace

TEST_F(EslEnvConfigTest, PasswordAndSipProxyComeFromTheEnvironment) {
    ::setenv("FREESWITCH_ESL_PASSWORD", "from-env-secret", 1);
    ::setenv("SIP_PROXY_HOST", "10.1.2.3", 1);
    ::setenv("SIP_PROXY_PORT", "5070", 1);
    write_yaml("gateway:\n  esl:\n    enabled: true\n    sip_proxy_host: \"127.0.0.1\"\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());
    EXPECT_EQ(cfg.esl().password, "from-env-secret");
    EXPECT_EQ(cfg.esl().sip_proxy_host, "10.1.2.3");
    EXPECT_EQ(cfg.esl().sip_proxy_port, 5070);
}

TEST_F(EslEnvConfigTest, EnvironmentWinsOverYaml) {
    ::setenv("FREESWITCH_ESL_PASSWORD", "from-env", 1);
    write_yaml("gateway:\n  esl:\n    enabled: true\n    password: \"from-yaml\"\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());
    EXPECT_EQ(cfg.esl().password, "from-env");
}

TEST_F(EslEnvConfigTest, ConversationTargetAndRedisComeFromTheEnvironment) {
    ::setenv("FREESWITCH_ESL_PASSWORD", "p", 1);
    ::setenv("CONVERSATION_SVC_TARGET", "localhost:50051", 1);
    ::setenv("REDIS_URL", "redis://10.0.0.5:6380/0", 1);
    write_yaml("gateway:\n  conversation:\n    type: \"grpc\"\n    endpoint: \"localhost:10000\"\n"
               "  redis:\n    host: \"127.0.0.1\"\n    port: 6379\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());
    EXPECT_EQ(cfg.conversation().endpoint, "localhost:50051");
    EXPECT_EQ(cfg.gateway().redis.host, "10.0.0.5");
    EXPECT_EQ(cfg.gateway().redis.port, 6380);
}

TEST_F(EslEnvConfigTest, WebSocketListensOnLoopbackUnlessGatewayListenHostIsSet) {
    ::setenv("FREESWITCH_ESL_PASSWORD", "p", 1);
    write_yaml("gateway:\n  websocket:\n    port: 8080\n");
    {
        voiceai::Config cfg;
        cfg.load(tmp_yaml_.string());
        EXPECT_EQ(cfg.websocket().host, "127.0.0.1");
    }
    ::setenv("GATEWAY_LISTEN_HOST", "192.168.0.116", 1);
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());
    EXPECT_EQ(cfg.websocket().host, "192.168.0.116");
}

TEST_F(EslEnvConfigTest, RedisUrlWithoutPortOrDbUsesDefaults) {
    ::setenv("FREESWITCH_ESL_PASSWORD", "p", 1);
    ::setenv("REDIS_URL", "redis://localhost", 1);
    write_yaml("gateway:\n  esl:\n    enabled: true\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());
    EXPECT_EQ(cfg.gateway().redis.host, "localhost");
    EXPECT_EQ(cfg.gateway().redis.port, 6379);
}

TEST_F(EslEnvConfigTest, UnsupportedRedisUrlRefusesToStart) {
    ::setenv("FREESWITCH_ESL_PASSWORD", "p", 1);
    write_yaml("gateway:\n  esl:\n    enabled: true\n");
    for (const char* bad : {"http://localhost:6379", "redis://:secret@localhost:6379", "redis://localhost:6379/2",
                            "redis://localhost:99999", "redis://:6379"}) {
        ::setenv("REDIS_URL", bad, 1);
        voiceai::Config cfg;
        EXPECT_THROW(cfg.load(tmp_yaml_.string()), std::runtime_error) << bad;
    }
}

TEST_F(EslEnvConfigTest, OutOfRangeOrMalformedPortRefusesToStart) {
    write_yaml("gateway:\n  esl:\n    enabled: true\n    password: \"p\"\n");
    for (const char* bad : {"80220", "8022x", "0", "-1", "99999999999999999999"}) {
        ::setenv("FREESWITCH_ESL_PORT", bad, 1);
        voiceai::Config cfg;
        EXPECT_THROW(cfg.load(tmp_yaml_.string()), std::runtime_error) << bad;
    }
    ::unsetenv("FREESWITCH_ESL_PORT");
    ::setenv("SIP_PROXY_PORT", "70000", 1);
    voiceai::Config cfg;
    EXPECT_THROW(cfg.load(tmp_yaml_.string()), std::runtime_error);
}

TEST_F(EslEnvConfigTest, EnabledEslWithoutAPasswordRefusesToStart) {
    write_yaml("gateway:\n  esl:\n    enabled: true\n");
    voiceai::Config cfg;
    EXPECT_THROW(cfg.load(tmp_yaml_.string()), std::runtime_error);
}

TEST_F(EslEnvConfigTest, DisabledEslNeedsNoPassword) {
    write_yaml("gateway:\n  esl:\n    enabled: false\n");
    voiceai::Config cfg;
    EXPECT_NO_THROW(cfg.load(tmp_yaml_.string()));
}

// Shipped files must leave the proxy host unset (update_kamailio_ip.sh writes it);
// an .env.example default would override the yaml and bypass the unset refusal.
TEST_F(EslEnvConfigTest, ShippedConfigAndEnvExampleLeaveNumbersRefused) {
    const std::filesystem::path src{YUVIZ_SOURCE_DIR};
    std::ifstream example{src / ".env.example"};
    ASSERT_TRUE(example.is_open());
    std::string line, shipped;
    bool found = false;
    while (std::getline(example, line)) {
        if (line.rfind("SIP_PROXY_HOST=", 0) == 0) {
            shipped = line.substr(std::string("SIP_PROXY_HOST=").size());
            found = true;
        }
    }
    ASSERT_TRUE(found) << ".env.example has no SIP_PROXY_HOST line";

    // What start_local.sh would hand the Gateway (a blank value is exported
    // as-is here; _load_env skips it, and env_value() must treat it as unset).
    ::setenv("SIP_PROXY_HOST", shipped.c_str(), 1);
    ::setenv("FREESWITCH_ESL_PASSWORD", "p", 1);
    voiceai::Config cfg;
    cfg.load((src / "config" / "gateway.yaml").string());
    EXPECT_EQ(cfg.esl().sip_proxy_host, "");

    voiceai::Logger logger = voiceai::Logger::make_null();
    voiceai::EslClient client{cfg.esl(), logger};
    std::string error;
    EXPECT_FALSE(client.transfer(
        voiceai::TransferRequest{"call-uuid-1", "cold", "1005", "x"}, error));
    EXPECT_EQ(error, "sip_proxy_host_unset");
}

TEST_F(EslEnvConfigTest, BlankSipProxyHostEnvDoesNotOverrideTheYaml) {
    ::setenv("FREESWITCH_ESL_PASSWORD", "p", 1);
    ::setenv("SIP_PROXY_HOST", "", 1);
    write_yaml("gateway:\n  esl:\n    enabled: true\n    sip_proxy_host: \"10.9.9.9\"\n");
    voiceai::Config cfg;
    cfg.load(tmp_yaml_.string());
    EXPECT_EQ(cfg.esl().sip_proxy_host, "10.9.9.9");
}
