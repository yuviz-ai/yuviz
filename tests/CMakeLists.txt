add_executable(gateway_tests
    config_test.cpp
    ringbuffer_test.cpp
    call_fsm_test.cpp
    vad_test.cpp
    silero_vad_test.cpp
    playback_queue_test.cpp
    thread_pool_test.cpp
    pending_metadata_test.cpp
    esl_client_test.cpp
    websocket_bind_test.cpp
    websocket_health_test.cpp
    transfer_correlator_test.cpp
    esl_event_listener_test.cpp
    cold_transfer_coordinator_test.cpp
    warm_transfer_coordinator_test.cpp
)

target_link_libraries(gateway_tests
    PRIVATE
        gateway_lib
        GTest::GTest
        GTest::Main
)

target_compile_features(gateway_tests PRIVATE cxx_std_20)
# Lets tests read the shipped config/gateway.yaml and .env.example, so a
# default that ships in those files is what gets tested.
target_compile_definitions(gateway_tests PRIVATE YUVIZ_SOURCE_DIR="${CMAKE_SOURCE_DIR}")

include(GoogleTest)
gtest_discover_tests(gateway_tests)
