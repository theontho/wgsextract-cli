add_library(wgse_metal_common STATIC runtime.mm)
target_include_directories(wgse_metal_common PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}")
target_compile_options(wgse_metal_common PRIVATE -fobjc-arc -Wall -Wextra -Werror)
target_link_libraries(wgse_metal_common PUBLIC "-framework Foundation" "-framework Metal")
