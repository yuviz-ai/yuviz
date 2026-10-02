#pragma once

namespace voiceai {

// Inherit privately to make a class non-movable.
class NonMovable {
protected:
    NonMovable()  = default;
    ~NonMovable() = default;

    NonMovable(NonMovable&&)            = delete;
    NonMovable& operator=(NonMovable&&) = delete;
};

} // namespace voiceai
