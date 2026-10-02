#include "dispatcher/Dispatcher.h"
#include "common/ThreadUtils.h"

namespace voiceai {

Dispatcher::Dispatcher(IMetrics& metrics, Logger& logger)
    : metrics_(metrics)
    , logger_(logger)
{}

Dispatcher::~Dispatcher() {
    stop();
}

bool Dispatcher::initialize() {
    return true;
}

bool Dispatcher::start() {
    if (running_.exchange(true)) return true;
    worker_ = std::thread([this] { dispatch_loop(); });
    logger_.info("Dispatcher started");
    return true;
}

void Dispatcher::stop() {
    if (!running_.exchange(false)) return;
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
    logger_.info("Dispatcher stopped");
}

void Dispatcher::shutdown() {}

void Dispatcher::dispatch(SessionEvent event) {
    {
        std::lock_guard lock{queue_mutex_};
        queue_.push(std::move(event));
    }
    cv_.notify_one();
}

IDispatcher::SubscriberId Dispatcher::subscribe(Subscriber handler) {
    const SubscriberId id = next_id_.fetch_add(1, std::memory_order_relaxed);
    {
        std::lock_guard lock{sub_mutex_};
        subscribers_.emplace(id, std::move(handler));
        cache_dirty_ = true;
    }
    return id;
}

void Dispatcher::unsubscribe(SubscriberId id) {
    std::lock_guard lock{sub_mutex_};
    subscribers_.erase(id);
    cache_dirty_ = true;
}

void Dispatcher::dispatch_loop() {
    set_thread_name("Dispatcher");

    while (running_.load(std::memory_order_acquire)) {
        SessionEvent event;
        {
            std::unique_lock lock{queue_mutex_};
            cv_.wait(lock, [this] {
                return !running_.load(std::memory_order_relaxed) || !queue_.empty();
            });
            if (!running_.load(std::memory_order_relaxed) && queue_.empty()) return;
            event = std::move(queue_.front());
            queue_.pop();
        }
        metrics_.increment("dispatcher.events");
        deliver(event);
    }
}

void Dispatcher::deliver(const SessionEvent& event) {
    // Only this thread touches subscriber_cache_, so iterating it unlocked is safe.
    {
        std::lock_guard lock{sub_mutex_};
        if (cache_dirty_) {
            subscriber_cache_.clear();
            subscriber_cache_.reserve(subscribers_.size());
            for (const auto& [id, fn] : subscribers_)
                subscriber_cache_.push_back(fn);
            cache_dirty_ = false;
        }
    }
    for (const auto& fn : subscriber_cache_) fn(event);
}

} // namespace voiceai
