#pragma once

#include <string>

namespace voiceai {

// Sets the OS-visible name of the calling thread (truncated to 15 bytes on Linux).
void set_thread_name(const std::string& name);

} // namespace voiceai
