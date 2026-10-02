#pragma once

namespace voiceai {

// Inherit privately to make a class non-copyable.
class NonCopyable {
protected:
    NonCopyable()  = default;
    ~NonCopyable() = default;

    NonCopyable(const NonCopyable&)            = delete;
    NonCopyable& operator=(const NonCopyable&) = delete;
};

} // namespace voiceai
