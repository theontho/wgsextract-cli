import com.intel.gkl.pairhmm.IntelPairHmm;
import org.broadinstitute.gatk.nativebindings.pairhmm.PairHMMNativeArguments;

/** Reports whether GKL's PairHMM initialisation leaves flush-to-zero on the calling JVM thread. */
public final class JavaThreadFtzProbe {
    private static float quarter(float value) {
        return value * 0.25f;
    }

    private static String state() {
        // Float.MIN_NORMAL / 4 is subnormal (0x200000) unless the thread flushes it to zero.
        double d = Double.MIN_NORMAL;
        return String.format("float=%08x double=%016x",
                Float.floatToRawIntBits(quarter(Float.MIN_NORMAL)),
                Double.doubleToRawLongBits(d * 0.25));
    }

    public static void main(String[] argv) throws Exception {
        System.out.println("jvm_start " + state());
        IntelPairHmm hmm = new IntelPairHmm();
        if (!hmm.load(null)) {
            throw new IllegalStateException("Native PairHMM unavailable");
        }
        System.out.println("after_load " + state());
        PairHMMNativeArguments args = new PairHMMNativeArguments();
        args.maxNumberOfThreads = 1;
        hmm.initialize(args);
        System.out.println("after_initialize " + state());
        Thread other = new Thread(() -> System.out.println("new_thread " + state()));
        other.start();
        other.join();
        hmm.done();
        System.out.println("after_done " + state());
    }
}
