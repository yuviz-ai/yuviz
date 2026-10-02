#include "telephony/EslEventListener.h"
#include "telephony/EslFraming.h"

#include <arpa/inet.h>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>

namespace voiceai {

namespace {

using esl_framing::parse_content_length;
using esl_framing::read_exact;
using esl_framing::read_until_blank_line;

// Waits indefinitely for a full header block (events are sparse), rechecking
// `running` every poll_interval so stop() takes effect promptly.
bool wait_for_frame_header(int fd, std::string& carry, std::string& out,
                           const std::atomic<bool>& running) {
    constexpr auto poll_interval = std::chrono::milliseconds{500};
    for (;;) {
        const auto pos = carry.find("\n\n");
        if (pos != std::string::npos) {
            out = carry.substr(0, pos);
            carry.erase(0, pos + 2);
            return true;
        }
        if (!running.load(std::memory_order_relaxed)) return false;

        pollfd pfd{fd, POLLIN, 0};
        const int rc = ::poll(&pfd, 1, static_cast<int>(poll_interval.count()));
        if (rc < 0) return false;   // poll error — connection likely broken
        if (rc == 0) continue;      // timeout, no data yet — loop, recheck running

        char buf[4096];
        const ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) return false;   // peer closed or real error
        carry.append(buf, static_cast<size_t>(n));
    }
}

// event-plain bodies are newline-separated "Key: value" lines.
std::string parse_header_value(const std::string& body, const std::string& key) {
    const std::string search = key + ": ";
    size_t pos = 0;
    while (pos < body.size()) {
        size_t eol = body.find('\n', pos);
        if (eol == std::string::npos) eol = body.size();
        if (eol - pos > search.size() && body.compare(pos, search.size(), search) == 0) {
            std::string value = body.substr(pos + search.size(), eol - pos - search.size());
            if (!value.empty() && value.back() == '\r') value.pop_back();
            return value;
        }
        pos = eol + 1;
    }
    return {};
}

// BACKGROUND_JOB bodies are doubly framed: inner headers, blank line, then
// "+OK <uuid>" or "-ERR <cause>". Strips "+OK " so callers get a bare uuid.
std::string parse_background_job_result(const std::string& body) {
    const auto sep = body.find("\n\n");
    if (sep == std::string::npos) return {};
    std::string result = body.substr(sep + 2);
    while (!result.empty() && (result.back() == '\n' || result.back() == '\r'))
        result.pop_back();
    if (result.rfind("+OK", 0) == 0) {
        result.erase(0, 3);
        while (!result.empty() && result.front() == ' ')
            result.erase(0, 1);
    }
    return result;
}

} // namespace

EslEventListener::EslEventListener(EslConfig cfg, Logger& logger, HangupHandler on_hangup,
                                    DtmfHandler on_dtmf,
                                    TransferCorrelator& transfer_correlator,
                                    TransferCorrelator& job_correlator)
    : cfg_(std::move(cfg))
    , logger_(logger)
    , on_hangup_(std::move(on_hangup))
    , on_dtmf_(std::move(on_dtmf))
    , transfer_correlator_(transfer_correlator)
    , job_correlator_(job_correlator)
{}

EslEventListener::~EslEventListener() {
    stop();
}

bool EslEventListener::start() {
    if (!cfg_.enabled) {
        logger_.info("EslEventListener: disabled (esl.enabled=false) — caller-hangup detection "
                     "relies solely on no_speech_timeout");
        return true;   // not a startup failure — same "degrade gracefully" posture as EslClient
    }
    if (running_.exchange(true)) return true;
    worker_ = std::thread([this] { run_loop(); });
    logger_.info("EslEventListener started host={} port={}", cfg_.host, cfg_.port);
    return true;
}

void EslEventListener::stop() {
    if (!running_.exchange(false)) return;
    if (worker_.joinable()) worker_.join();
    if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
    logger_.info("EslEventListener stopped");
}

bool EslEventListener::connect_and_subscribe() {
    addrinfo hints{};
    hints.ai_family   = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    const std::string port_str = std::to_string(cfg_.port);
    if (::getaddrinfo(cfg_.host.c_str(), port_str.c_str(), &hints, &res) != 0 || !res) {
        logger_.warn("EslEventListener: getaddrinfo failed host={} port={}", cfg_.host, cfg_.port);
        return false;
    }

    const int fd = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) {
        ::freeaddrinfo(res);
        logger_.warn("EslEventListener: socket() failed errno={}", errno);
        return false;
    }

    const int rc = ::connect(fd, res->ai_addr, res->ai_addrlen);
    ::freeaddrinfo(res);
    if (rc < 0) {
        logger_.warn("EslEventListener: connect() failed host={} port={} errno={}",
                     cfg_.host, cfg_.port, errno);
        ::close(fd);
        return false;
    }

    const auto timeout = std::chrono::milliseconds{cfg_.connect_timeout_ms};
    std::string carry, headers;
    if (!read_until_blank_line(fd, carry, headers, timeout) ||
        headers.find("auth/request") == std::string::npos) {
        logger_.warn("EslEventListener: did not receive auth/request from {}:{}", cfg_.host, cfg_.port);
        ::close(fd);
        return false;
    }

    const std::string auth_cmd = "auth " + cfg_.password + "\n\n";
    if (::send(fd, auth_cmd.data(), auth_cmd.size(), 0) < 0) {
        logger_.warn("EslEventListener: send(auth) failed errno={}", errno);
        ::close(fd);
        return false;
    }
    std::string auth_reply;
    if (!read_until_blank_line(fd, carry, auth_reply, timeout) ||
        auth_reply.find("+OK") == std::string::npos) {
        logger_.warn("EslEventListener: auth rejected by {}:{} (check esl.password in gateway.yaml)",
                     cfg_.host, cfg_.port);
        ::close(fd);
        return false;
    }

    const std::string sub_cmd =
        "event plain CHANNEL_HANGUP CHANNEL_BRIDGE CHANNEL_ANSWER BACKGROUND_JOB DTMF\n\n";
    if (::send(fd, sub_cmd.data(), sub_cmd.size(), 0) < 0) {
        logger_.warn("EslEventListener: send(event subscribe) failed errno={}", errno);
        ::close(fd);
        return false;
    }
    std::string sub_reply;
    if (!read_until_blank_line(fd, carry, sub_reply, timeout) ||
        sub_reply.find("+OK") == std::string::npos) {
        logger_.warn("EslEventListener: event subscription rejected by {}:{} reply={}",
                     cfg_.host, cfg_.port, sub_reply);
        ::close(fd);
        return false;
    }

    fd_ = fd;
    read_carry_ = std::move(carry);  // any bytes read past the subscribe reply (normally none)
    logger_.info("EslEventListener: connected and subscribed to CHANNEL_HANGUP/CHANNEL_BRIDGE/"
                 "CHANNEL_ANSWER/BACKGROUND_JOB host={} port={}", cfg_.host, cfg_.port);
    return true;
}

void EslEventListener::run_loop() {
    while (running_.load(std::memory_order_relaxed)) {
        if (fd_ < 0 && !connect_and_subscribe()) {
            // Reconnect backoff, rechecked against running_ in small steps
            // so stop() isn't delayed by a long uninterruptible sleep.
            for (int i = 0; i < 20 && running_.load(std::memory_order_relaxed); ++i)
                std::this_thread::sleep_for(std::chrono::milliseconds{100});
            continue;
        }

        std::string headers;
        if (!wait_for_frame_header(fd_, read_carry_, headers, running_)) {
            if (!running_.load(std::memory_order_relaxed)) break;
            logger_.warn("EslEventListener: connection lost, reconnecting host={} port={}",
                         cfg_.host, cfg_.port);
            ::close(fd_);
            fd_ = -1;
            read_carry_.clear();
            continue;
        }

        const size_t content_length = parse_content_length(headers);
        if (content_length == 0) continue;  // not an event-plain frame — nothing to act on

        std::string body;
        if (!read_exact(fd_, read_carry_, body, content_length, std::chrono::milliseconds{2000})) {
            logger_.warn("EslEventListener: incomplete event body, reconnecting");
            ::close(fd_);
            fd_ = -1;
            read_carry_.clear();
            continue;
        }

        const std::string event_name = parse_header_value(body, "Event-Name");

        // BACKGROUND_JOB has no Unique-ID; it must be handled before that check.
        if (event_name == "BACKGROUND_JOB") {
            const std::string job_uuid = parse_header_value(body, "Job-UUID");
            if (job_uuid.empty()) continue;
            const std::string result = parse_background_job_result(body);
            const bool success = !result.empty() && result.rfind("-ERR", 0) != 0;
            if (job_correlator_.resolve(job_uuid, success, result))
                logger_.info("EslEventListener: BACKGROUND_JOB job_uuid={} success={} result={}",
                             job_uuid, success, result);
            continue;
        }

        const std::string uuid = parse_header_value(body, "Unique-ID");
        if (uuid.empty()) continue;

        if (event_name == "CHANNEL_BRIDGE") {
            if (transfer_correlator_.resolve(uuid, /*success=*/true, "bridged"))
                logger_.info("EslEventListener: CHANNEL_BRIDGE uuid={} — transfer succeeded", uuid);
        } else if (event_name == "CHANNEL_ANSWER") {
            if (transfer_correlator_.resolve(uuid, /*success=*/true, "answered"))
                logger_.info("EslEventListener: CHANNEL_ANSWER uuid={} — agent leg answered", uuid);
        } else if (event_name == "CHANNEL_HANGUP") {
            // A pending transfer's failure outcome; the transfer path already
            // tears the session down, so on_hangup_ is skipped.
            if (transfer_correlator_.resolve(uuid, /*success=*/false, "hangup_before_bridge")) {
                logger_.info("EslEventListener: CHANNEL_HANGUP uuid={} — transfer failed "
                             "(hung up before bridging)", uuid);
            } else {
                logger_.info("EslEventListener: CHANNEL_HANGUP uuid={}", uuid);
                if (on_hangup_) on_hangup_(uuid);
            }
        } else if (event_name == "DTMF") {
            const std::string digit = parse_header_value(body, "DTMF-Digit");
            if (!digit.empty() && on_dtmf_) {
                logger_.info("EslEventListener: DTMF received uuid={}", uuid);
                on_dtmf_(uuid, digit);
            }
        }
    }
}

} // namespace voiceai
