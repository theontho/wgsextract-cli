// Print the default Metal device's properties as key=value lines.
import Metal

guard let device = MTLCreateSystemDefaultDevice() else {
    print("metal_device=none")
    exit(0)
}
print("metal_device=\(device.name)")
print("metal_recommended_max_working_set_bytes=\(device.recommendedMaxWorkingSetSize)")
print("metal_max_buffer_bytes=\(device.maxBufferLength)")
print("metal_unified_memory=\(device.hasUnifiedMemory)")
print("metal_apple7=\(device.supportsFamily(.apple7))")
