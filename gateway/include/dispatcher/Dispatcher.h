#pragma once

#include "dispatcher/IDispatcher.h"
#include "metrics/IMetrics.h"
#include "logging/Logger.h"
#include "common/NonCopyable.h"
#include "common/NonMovable.h"

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <queue>
#include <thread>
#include <unordered_map>
#include <vector>

namespace voiceai {

class Dispatcher final : public IDispatcher, private NonCopyable, private NonMovable {
public:
    Dispatcher(IMetrics& metrics, Logger& logger);
    ~Dispatcher() override;

    bool initialize() override;
    bool start()      override;
    void stop()       override;
    void shutdown()   override;

    void         dispatch(SessionEvent event) override;
    SubscriberId subscribe(Subscriber handler) override;
    void         unsubscribe(SubscriberId id)  override;

private:
    void dispatch_loop();
    void deliver(const SessionEvent& event);

    IMetrics&               metrics_;
    Logger&                 logger_;

    std::thread             worker_;
    std::atomic<bool>       running_{false};
    std::mutex              queue_mutex_;
    std::condition_variable cv_;
    std::queue<SessionEvent> queue_;

    mutable std::mutex                               sub_mutex_;
    std::unordered_map<SubscriberId, Subscriber>     subscribers_;
    std::atomic<SubscriberId>                        next_id_{1};
    // Rebuilt on subscribe/unsubscribe; only touched by the Dispatcher thread.
    std::vector<Subscriber>                          subscriber_cache_;
    bool                                             cache_dirty_{true};
};

} // namespace voiceai
