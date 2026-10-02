#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <optional>

namespace voiceai {

// Lock-free bounded SPSC queue: exactly one producer thread and one consumer
// thread, otherwise UB. push() drops and returns false when full.
template<typename T, size_t N>
class SPSCQueue {
    static_assert(N >= 2 && (N & (N - 1)) == 0, "N must be a power of two >= 2");

    std::array<T, N>                buf_{};
    alignas(64) std::atomic<size_t> head_{0};  // owned by consumer
    alignas(64) std::atomic<size_t> tail_{0};  // owned by producer

public:
    // Producer thread only.
    bool push(T item) noexcept {
        const size_t t = tail_.load(std::memory_order_relaxed);
        if (t - head_.load(std::memory_order_acquire) >= N)
            return false;
        buf_[t & (N - 1)] = std::move(item);
        tail_.store(t + 1, std::memory_order_release);
        return true;
    }

    // Consumer thread only.
    std::optional<T> pop() noexcept {
        const size_t h = head_.load(std::memory_order_relaxed);
        if (tail_.load(std::memory_order_acquire) == h)
            return std::nullopt;
        T item = std::move(buf_[h & (N - 1)]);
        head_.store(h + 1, std::memory_order_release);
        return item;
    }
};

} // namespace voiceai
