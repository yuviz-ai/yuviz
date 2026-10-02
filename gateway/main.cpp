#include "application/Application.h"
#include "common/ThreadUtils.h"
#include "config/Config.h"
#include "transport/GrpcConversationTransport.h"

#include <grpcpp/grpcpp.h>

#include <cstdlib>
#include <iostream>
#include <string>

int main(int argc, char* argv[]) {
    voiceai::set_thread_name("GatewayMain");

    const std::string config_path = (argc > 1)
        ? argv[1]
        : "config/gateway.yaml";

    try {
        voiceai::Application app{config_path};

        // Registered here to keep gRPC out of gateway_lib (gateway_tests can't link it under ASan).
        {
            voiceai::Config cfg;
            cfg.load(config_path);
            const auto& conv = cfg.conversation();

            if (conv.type == "grpc") {
                // One channel shared across all sessions — HTTP/2 multiplexed.
                auto channel = grpc::CreateChannel(
                    conv.endpoint, grpc::InsecureChannelCredentials());

                app.transport_factory().register_provider(
                    "grpc",
                    [ch = std::move(channel)](voiceai::Logger& lg) {
                        return std::make_unique<voiceai::GrpcConversationTransport>(ch, lg);
                    });
            }
        }

        return app.run();

    } catch (const std::exception& e) {
        std::cerr << "Fatal: " << e.what() << '\n';
        return EXIT_FAILURE;
    }
}
