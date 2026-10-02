#pragma once

#include "websocket/IWebSocketConnection.h"
#include <atomic>
#include <mutex>
#include <queue>
#include <string>
#include <vector>
#include <libwebsockets.h>

namespace voiceai {

struct PendingWrite {
    std::vector<uint8_t> buf;  // LWS_PRE bytes prepended
    lws_write_protocol   proto;
};

class WebSocketConnection final : public IWebSocketConnection {
public:
    WebSocketConnection(struct lws* wsi, std::string id, std::string path);
    ~WebSocketConnection() override;

    WebSocketConnection(const WebSocketConnection&)            = delete;
    WebSocketConnection& operator=(const WebSocketConnection&) = delete;

    [[nodiscard]] const std::string& id()      const noexcept override { return id_; }
    [[nodiscard]] const std::string& path()    const noexcept override { return path_; }
    [[nodiscard]] bool               is_open() const noexcept override { return open_.load(); }

    void send_text(const std::string& msg)           override;
    void send_binary(const uint8_t* data, size_t n)  override;
    void close()                                      override;

    void set_on_binary(BinaryHandler handler) override;
    void set_on_text(TextHandler handler)     override;
    void set_on_close(CloseHandler handler)   override;

    // Called from the lws service thread.
    void handle_receive(const uint8_t* data, size_t n, bool is_binary);
    void handle_close();
    void flush_pending_writes();

    // Thread-safe.
    [[nodiscard]] bool has_pending_writes() const noexcept;

private:
    void enqueue(std::vector<uint8_t> buf_with_pre, lws_write_protocol proto);

    struct lws*        wsi_;
    std::string        id_;
    std::string        path_;
    std::atomic<bool>  open_{true};

    BinaryHandler on_binary_;
    TextHandler   on_text_;
    CloseHandler  on_close_;

    mutable std::mutex        write_mutex_;
    std::queue<PendingWrite>  write_queue_;
};

} // namespace voiceai
