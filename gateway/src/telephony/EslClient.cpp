#include "telephony/EslClient.h"
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

bool looks_like_sip_uri(const std::string& destination) {
    return destination.rfind("sip:", 0) == 0 || destination.rfind("sips:", 0) == 0;
}

bool is_ascii_alnum(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
}

bool is_dial_number(const std::string& s) {
    const size_t start = (!s.empty() && s[0] == '+') ? 1 : 0;
    const size_t digits = s.size() - start;
    if (digits < 2 || digits > 15) return false;
    for (size_t i = start; i < s.size(); ++i) {
        if (s[i] < '0' || s[i] > '9') return false;
    }
    return true;
}

// Tenant-configured values are pasted into ESL commands: anything that could
// end the command (newline), split app args (space), close a quoted arg ('),
// or open a channel-variable block ({ , }) must never get through.
bool is_safe_sip_uri(const std::string& s) {
    const size_t scheme = s.rfind("sips:", 0) == 0 ? 5 : (s.rfind("sip:", 0) == 0 ? 4 : 0);
    if (scheme == 0) return false;
    const auto at = s.find('@', scheme);
    if (at == std::string::npos || at == scheme || at + 1 == s.size()) return false;
    if (s.find('@', at + 1) != std::string::npos) return false;
    for (size_t i = scheme; i < s.size(); ++i) {
        const char c = s[i];
        if (!is_ascii_alnum(c) && c != '@' && c != '.' && c != '-' && c != '_' &&
            c != '+' && c != ':' && c != ';' && c != '=' && c != '~') {
            return false;
        }
    }
    return true;
}

// The platform's own AI entry numbers (scripts/freeswitch/00_voice_ai.xml,
// scripts/kamailio/kamailio.cfg.tpl). Bridged from FreeSWITCH they hit
// Kamailio's loop guard, so a transfer to one can never connect.
bool is_platform_ai_number(const std::string& s) {
    return s == "788" || (s.size() == 4 && s.rfind("500", 0) == 0 && s[3] >= '0' && s[3] <= '9');
}

std::string to_lower(std::string s) {
    for (char& c : s) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    return s;
}

// Host part of an already-validated SIP URI: after '@', up to ':' or ';'.
std::string sip_uri_host(const std::string& uri) {
    const auto at = uri.find('@');
    const auto end = uri.find_first_of(":;", at + 1);
    std::string host = uri.substr(at + 1, end == std::string::npos ? std::string::npos : end - at - 1);
    if (!host.empty() && host.back() == '.') host.pop_back();
    return to_lower(std::move(host));
}

// inet_aton, like the resolver, also accepts "127.1", "0x7f.1" and "2130706433".
bool parse_ipv4(const std::string& host, in_addr& out) {
    return !host.empty() && ::inet_aton(host.c_str(), &out) != 0;
}

bool is_loopback_or_any(const std::string& host) {
    in_addr addr{};
    if (parse_ipv4(host, addr)) {
        const uint32_t a = ntohl(addr.s_addr);
        return (a >> 24) == 127 || a == 0;
    }
    const std::string suffix = ".localhost";
    return host == "localhost" ||
           (host.size() > suffix.size() &&
            host.compare(host.size() - suffix.size(), suffix.size(), suffix) == 0);
}

bool is_same_host(const std::string& host, const std::string& platform_host) {
    if (platform_host.empty()) return false;
    in_addr a{}, b{};
    if (parse_ipv4(host, a) && parse_ipv4(platform_host, b)) return a.s_addr == b.s_addr;
    std::string p = to_lower(platform_host);
    if (!p.empty() && p.back() == '.') p.pop_back();
    return host == p;
}

bool is_safe_uuid(const std::string& s) {
    if (s.empty() || s.size() > 64) return false;
    for (const char c : s) {
        if (!is_ascii_alnum(c) && c != '-') return false;
    }
    return true;
}

bool is_safe_destination(const std::string& destination) {
    return looks_like_sip_uri(destination) ? is_safe_sip_uri(destination)
                                           : is_dial_number(destination);
}

// bgapi's reply has no body; the Job-UUID is its own header line.
bool parse_job_uuid(const std::string& headers, std::string& out_uuid) {
    const std::string key = "\nJob-UUID:";
    auto pos = headers.rfind(key);
    size_t value_start;
    if (pos != std::string::npos) {
        value_start = pos + key.size();
    } else if (headers.rfind("Job-UUID:", 0) == 0) {
        value_start = std::string("Job-UUID:").size();
    } else {
        return false;
    }
    while (value_start < headers.size() && headers[value_start] == ' ') ++value_start;
    const auto end = headers.find('\n', value_start);
    out_uuid = headers.substr(value_start, end == std::string::npos
                                            ? std::string::npos : end - value_start);
    return !out_uuid.empty();
}

} // namespace

EslClient::EslClient(EslConfig cfg, Logger& logger)
    : cfg_(std::move(cfg))
    , logger_(logger)
{
    if (cfg_.enabled && cfg_.sip_proxy_host.empty()) {
        logger_.error("EslClient: esl.sip_proxy_host is not set — every transfer to a number "
                      "will be refused. Run scripts/update_kamailio_ip.sh, which writes "
                      "SIP_PROXY_HOST (the IP Kamailio listens on) into .env, then restart "
                      "the Gateway.");
    }
}

EslClient::~EslClient() {
    std::lock_guard lock{mutex_};
    disconnect_locked();
}

void EslClient::disconnect_locked() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
    read_buf_.clear();
}

bool EslClient::ensure_connected_locked() {
    if (fd_ >= 0) return true;

    addrinfo hints{};
    hints.ai_family   = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    const std::string port_str = std::to_string(cfg_.port);
    if (::getaddrinfo(cfg_.host.c_str(), port_str.c_str(), &hints, &res) != 0 || !res) {
        logger_.warn("EslClient: getaddrinfo failed host={} port={}", cfg_.host, cfg_.port);
        return false;
    }

    const int fd = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) {
        ::freeaddrinfo(res);
        logger_.warn("EslClient: socket() failed errno={}", errno);
        return false;
    }

    // Non-blocking connect with an explicit timeout — a plain connect() can
    // block far longer than acceptable against an unreachable/firewalled host.
    ::fcntl(fd, F_SETFL, O_NONBLOCK);
    const int rc = ::connect(fd, res->ai_addr, res->ai_addrlen);
    ::freeaddrinfo(res);

    if (rc < 0 && errno != EINPROGRESS) {
        logger_.warn("EslClient: connect() failed host={} port={} errno={}",
                     cfg_.host, cfg_.port, errno);
        ::close(fd);
        return false;
    }
    if (rc != 0) {
        pollfd pfd{fd, POLLOUT, 0};
        const int pr = ::poll(&pfd, 1, static_cast<int>(cfg_.connect_timeout_ms));
        int soerr = 0;
        socklen_t len = sizeof(soerr);
        if (pr <= 0 || ::getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &len) != 0 || soerr != 0) {
            logger_.warn("EslClient: connect timed out/failed host={} port={}",
                         cfg_.host, cfg_.port);
            ::close(fd);
            return false;
        }
    }
    // Blocking is fine: the read helpers bound their own timeouts via poll().
    const int flags = ::fcntl(fd, F_GETFL, 0);
    ::fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);

    const auto timeout = std::chrono::milliseconds{cfg_.connect_timeout_ms};
    std::string carry, headers;
    if (!read_until_blank_line(fd, carry, headers, timeout) ||
        headers.find("auth/request") == std::string::npos) {
        logger_.warn("EslClient: did not receive auth/request from {}:{}", cfg_.host, cfg_.port);
        ::close(fd);
        return false;
    }

    const std::string auth_cmd = "auth " + cfg_.password + "\n\n";
    if (::send(fd, auth_cmd.data(), auth_cmd.size(), 0) < 0) {
        logger_.warn("EslClient: send(auth) failed errno={}", errno);
        ::close(fd);
        return false;
    }

    std::string auth_reply;
    if (!read_until_blank_line(fd, carry, auth_reply, timeout) ||
        auth_reply.find("+OK") == std::string::npos) {
        logger_.warn("EslClient: auth rejected by {}:{} (check esl.password in gateway.yaml)",
                     cfg_.host, cfg_.port);
        ::close(fd);
        return false;
    }

    fd_ = fd;
    read_buf_ = std::move(carry);   // any bytes read past the auth reply (normally none)
    logger_.info("EslClient: connected and authenticated host={} port={}", cfg_.host, cfg_.port);
    return true;
}

bool EslClient::send_command_locked(const std::string& command, std::string& reply_out) {
    // Backstop for every command: a line break ends an ESL command, so one
    // inside it would run whatever follows as a second command.
    if (command.find_first_of(std::string("\r\n\0", 3)) != std::string::npos) {
        logger_.error("EslClient: refusing to send a command containing CR/LF/NUL length={}",
                      command.size());
        return false;
    }
    if (!ensure_connected_locked()) return false;

    const std::string full = command + "\n\n";
    if (::send(fd_, full.data(), full.size(), 0) < 0) {
        logger_.warn("EslClient: send() failed errno={} command={}", errno, command);
        disconnect_locked();
        return false;
    }

    const auto timeout = std::chrono::milliseconds{cfg_.connect_timeout_ms};
    std::string headers;
    if (!read_until_blank_line(fd_, read_buf_, headers, timeout)) {
        logger_.warn("EslClient: no reply headers command={}", command);
        disconnect_locked();
        return false;
    }

    const size_t content_length = parse_content_length(headers);
    if (content_length == 0) {
        reply_out = headers;   // command/reply carries its result in Reply-Text
        return true;
    }

    std::string body;
    if (!read_exact(fd_, read_buf_, body, content_length, timeout)) {
        logger_.warn("EslClient: incomplete reply body command={}", command);
        disconnect_locked();
        return false;
    }
    reply_out = body;   // api/response carries its result in the body
    return true;
}

void EslClient::hangup(const std::string& uuid, const std::string& reason) {
    if (!cfg_.enabled) {
        logger_.info("EslClient: disabled (esl.enabled=false) — not hanging up SIP leg "
                     "uuid={} reason={}; WebSocket/audio already closed", uuid, reason);
        return;
    }
    if (uuid.empty()) {
        logger_.warn("EslClient: hangup requested with empty call_id — skipping "
                     "(mod_audio_fork's \"start\" callSid may not have arrived yet) reason={}",
                     reason);
        return;
    }
    if (!is_safe_uuid(uuid)) {
        logger_.warn("EslClient: hangup refused, call_id is not a plain uuid length={} reason={}",
                     uuid.size(), reason);
        return;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_kill " + uuid, reply)) {
        logger_.warn("EslClient: hangup failed uuid={} reason={} "
                     "(ESL unreachable or unauthenticated — call remains connected)",
                     uuid, reason);
        return;
    }
    if (reply.find("+OK") != std::string::npos) {
        logger_.info("EslClient: hangup issued uuid={} reason={}", uuid, reason);
    } else {
        logger_.warn("EslClient: hangup command rejected uuid={} reason={} reply={}",
                     uuid, reason, reply);
    }
}

bool EslClient::transfer(const TransferRequest& req, std::string& error_out) {
    const std::string& uuid        = req.call_id;
    const std::string& destination = req.destination;
    const std::string& reason      = req.reason;
    const std::string& transfer_id = req.transfer_id;

    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        logger_.info("EslClient: disabled (esl.enabled=false) — not transferring "
                     "uuid={} destination={} reason={}", uuid, destination, reason);
        return false;
    }
    if (uuid.empty()) {
        error_out = "empty_uuid";
        logger_.warn("EslClient: transfer requested with empty call_id — skipping "
                     "destination={} reason={}", destination, reason);
        return false;
    }
    if (destination.empty()) {
        error_out = "empty_destination";
        logger_.warn("EslClient: transfer requested with empty destination uuid={} reason={}",
                     uuid, reason);
        return false;
    }
    if (!is_safe_uuid(uuid)) {
        error_out = "invalid_uuid";
        logger_.warn("EslClient: transfer refused, call_id is not a plain uuid length={}",
                     uuid.size());
        return false;
    }
    if (const std::string problem = destination_problem(destination); !problem.empty()) {
        error_out = problem;
        if (problem == "invalid_destination") {
            logger_.warn("EslClient: transfer refused, destination is not a plain number or "
                         "sip URI uuid={} reason={} length={}", uuid, reason, destination.size());
        } else if (problem == "platform_destination") {
            logger_.warn("EslClient: transfer refused, destination is this platform's own "
                         "AI number or address uuid={} reason={} destination={}",
                         uuid, reason, destination);
        } else {
            logger_.error("EslClient: transfer refused, esl.sip_proxy_host is not set — "
                          "a number cannot be dialed (run scripts/update_kamailio_ip.sh) "
                          "uuid={} reason={}", uuid, reason);
        }
        return false;
    }

    // Always an inline bridge, never a dialplan context: FreeSWITCH's stock
    // "default" context maps numbers like 779 (eavesdrop all) and 886
    // (intercept) to features that reach other tenants' calls.
    const std::string command =
        "api uuid_transfer " + uuid + " 'bridge:" + dial_string_for(destination) + "' inline";

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked(command, reply)) {
        error_out = "esl_unreachable";
        logger_.warn("EslClient: transfer failed uuid={} destination={} reason={} "
                     "(ESL unreachable or unauthenticated — call remains connected)",
                     uuid, destination, reason);
        return false;
    }
    if (reply.find("+OK") != std::string::npos) {
        // Accepted, not succeeded: the outcome arrives via TransferCorrelator.
        logger_.info("EslClient: transfer command accepted uuid={} destination={} reason={} "
                     "transfer_id={} — awaiting CHANNEL_BRIDGE/CHANNEL_HANGUP to confirm outcome",
                     uuid, destination, reason, transfer_id);
        return true;
    }
    error_out = reply;
    logger_.warn("EslClient: transfer command rejected uuid={} destination={} reason={} reply={}",
                 uuid, destination, reason, reply);
    return false;
}

std::string EslClient::destination_problem(const std::string& destination) const {
    if (!is_safe_destination(destination)) return "invalid_destination";
    if (looks_like_sip_uri(destination)) {
        // A URI at the switch (or the proxy in front of it) would enter one of
        // FreeSWITCH's own dialplan contexts. maddr overrides the host.
        const std::string host = sip_uri_host(destination);
        if (host.empty()) return "invalid_destination";
        if (to_lower(destination).find(";maddr") != std::string::npos ||
            is_loopback_or_any(host) || is_same_host(host, cfg_.host) ||
            is_same_host(host, cfg_.sip_proxy_host)) {
            return "platform_destination";
        }
        return "";
    }
    if (is_platform_ai_number(destination)) return "platform_destination";
    if (cfg_.sip_proxy_host.empty()) return "sip_proxy_host_unset";
    return "";
}

std::string EslClient::dial_string_for(const std::string& destination) const {
    return looks_like_sip_uri(destination)
        ? "sofia/external/" + destination
        : "sofia/external/sip:" + destination + "@" + cfg_.sip_proxy_host + ":" +
              std::to_string(cfg_.sip_proxy_port);
}

bool EslClient::originate_async(const std::string& destination,
                                const std::string& caller_id_number,
                                std::string& out_job_uuid, std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        logger_.info("EslClient: disabled (esl.enabled=false) — not originating "
                     "destination={}", destination);
        return false;
    }
    if (destination.empty()) {
        error_out = "empty_destination";
        logger_.warn("EslClient: originate requested with empty destination");
        return false;
    }
    if (const std::string problem = destination_problem(destination); !problem.empty()) {
        error_out = problem;
        if (problem == "invalid_destination") {
            logger_.warn("EslClient: originate refused, destination is not a plain number or "
                         "sip URI length={}", destination.size());
        } else if (problem == "platform_destination") {
            logger_.warn("EslClient: originate refused, destination is this platform's own "
                         "AI number or address destination={}", destination);
        } else {
            logger_.error("EslClient: originate refused, esl.sip_proxy_host is not set — "
                          "a number cannot be dialed (run scripts/update_kamailio_ip.sh)");
        }
        return false;
    }
    // A bad caller id must not block the transfer (an anonymous ANI is legitimate);
    // it is dropped rather than interpolated.
    std::string caller_id_vars;
    if (is_dial_number(caller_id_number)) {
        caller_id_vars = "{origination_caller_id_number=" + caller_id_number + "}";
    } else if (!caller_id_number.empty()) {
        logger_.warn("EslClient: originate dropping caller id that is not a plain number "
                     "length={}", caller_id_number.size());
    }

    // Numbers go via the SIP proxy, which owns the registrar. Not loopback/
    // (parks before the real answer) nor user/ (phones don't register with FreeSWITCH).
    const std::string command =
        "bgapi originate " + caller_id_vars + dial_string_for(destination) + " &park()";

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked(command, reply)) {
        error_out = "esl_unreachable";
        logger_.warn("EslClient: originate failed destination={} "
                     "(ESL unreachable or unauthenticated)", destination);
        return false;
    }
    if (reply.find("+OK") == std::string::npos || !parse_job_uuid(reply, out_job_uuid)) {
        error_out = reply;
        logger_.warn("EslClient: originate command rejected destination={} reply={}",
                     destination, reply);
        return false;
    }
    logger_.info("EslClient: originate accepted destination={} job_uuid={} — "
                 "awaiting BACKGROUND_JOB/CHANNEL_ANSWER to confirm outcome",
                 destination, out_job_uuid);
    return true;
}

bool EslClient::bridge(const std::string& uuid_a, const std::string& uuid_b,
                       std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        return false;
    }
    if (uuid_a.empty() || uuid_b.empty()) {
        error_out = "empty_uuid";
        logger_.warn("EslClient: bridge requested with an empty uuid uuid_a={} uuid_b={}",
                     uuid_a, uuid_b);
        return false;
    }
    if (!is_safe_uuid(uuid_a) || !is_safe_uuid(uuid_b)) {
        error_out = "invalid_uuid";
        logger_.warn("EslClient: bridge refused, a uuid is not a plain uuid");
        return false;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_bridge " + uuid_a + " " + uuid_b, reply)) {
        error_out = "esl_unreachable";
        logger_.warn("EslClient: bridge failed uuid_a={} uuid_b={} "
                     "(ESL unreachable or unauthenticated)", uuid_a, uuid_b);
        return false;
    }
    if (reply.find("+OK") != std::string::npos) {
        logger_.info("EslClient: bridge command accepted uuid_a={} uuid_b={}", uuid_a, uuid_b);
        return true;
    }
    error_out = reply;
    logger_.warn("EslClient: bridge command rejected uuid_a={} uuid_b={} reply={}",
                 uuid_a, uuid_b, reply);
    return false;
}

bool EslClient::stop_audio_fork(const std::string& uuid, std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        return false;
    }
    if (uuid.empty()) {
        error_out = "empty_uuid";
        return false;
    }
    if (!is_safe_uuid(uuid)) {
        error_out = "invalid_uuid";
        return false;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_audio_fork " + uuid + " stop", reply)) {
        error_out = "esl_unreachable";
        logger_.warn("EslClient: stop_audio_fork failed uuid={} "
                     "(ESL unreachable or unauthenticated)", uuid);
        return false;
    }
    if (reply.find("+OK") != std::string::npos) {
        logger_.info("EslClient: audio fork stopped uuid={}", uuid);
        return true;
    }
    error_out = reply;
    logger_.warn("EslClient: stop_audio_fork rejected uuid={} reply={}", uuid, reply);
    return false;
}

bool EslClient::hold(const std::string& uuid, std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        return false;
    }
    if (uuid.empty()) {
        error_out = "empty_uuid";
        return false;
    }
    if (!is_safe_uuid(uuid)) {
        error_out = "invalid_uuid";
        return false;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_hold " + uuid, reply)) {
        error_out = "esl_unreachable";
        return false;
    }
    if (reply.find("+OK") != std::string::npos) return true;
    error_out = reply;
    logger_.warn("EslClient: hold rejected uuid={} reply={}", uuid, reply);
    return false;
}

bool EslClient::unhold(const std::string& uuid, std::string& error_out) {
    if (!cfg_.enabled) {
        error_out = "esl_disabled";
        return false;
    }
    if (uuid.empty()) {
        error_out = "empty_uuid";
        return false;
    }
    if (!is_safe_uuid(uuid)) {
        error_out = "invalid_uuid";
        return false;
    }

    std::lock_guard lock{mutex_};
    std::string reply;
    if (!send_command_locked("api uuid_hold off " + uuid, reply)) {
        error_out = "esl_unreachable";
        return false;
    }
    if (reply.find("+OK") != std::string::npos) return true;
    error_out = reply;
    logger_.warn("EslClient: unhold rejected uuid={} reply={}", uuid, reply);
    return false;
}

} // namespace voiceai
