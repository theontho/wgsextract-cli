import com.intel.gkl.IntelGKLUtils;
import com.intel.gkl.pairhmm.IntelPairHmm;
import com.intel.gkl.pairhmm.IntelPairHmmOMP;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import org.broadinstitute.gatk.nativebindings.pairhmm.HaplotypeDataHolder;
import org.broadinstitute.gatk.nativebindings.pairhmm.PairHMMNativeArguments;
import org.broadinstitute.gatk.nativebindings.pairhmm.ReadDataHolder;

/**
 * Checks that GKL's PairHMM leaves the JVM's floating-point state alone: after load,
 * initialize, computeLikelihoods and done, the calling thread and threads created
 * afterwards still compute subnormals, and GKL reports flush-to-zero off. Usage:
 * {@code JavaThreadFtzProbe THREADS}; 1 selects the single-threaded library and more the
 * OpenMP one. Run each in its own JVM, because JNI binds the shared native methods to
 * whichever library loads first. On aarch64 any leak fails the probe; elsewhere (stock
 * x86 GKL leaves FTZ set) it only reports.
 */
public final class JavaThreadFtzProbe {
    private static final List<String> failures = new ArrayList<>();
    private static IntelGKLUtils utils;

    private static float quarter(float value) {
        return value * 0.25f;
    }

    private static double quarter(double value) {
        return value * 0.25;
    }

    private static String state() {
        // MIN_NORMAL / 4 is subnormal unless the thread flushes it to zero.
        return String.format("float=%08x double=%016x ftz=%b",
                Float.floatToRawIntBits(quarter(Float.MIN_NORMAL)),
                Double.doubleToRawLongBits(quarter(Double.MIN_NORMAL)), utils.getFlushToZero());
    }

    private static final String CLEAN = "float=00200000 double=0004000000000000 ftz=false";

    private static void check(String stage) throws InterruptedException {
        String[] other = new String[1];
        Thread thread = new Thread(() -> other[0] = state());
        thread.start();
        thread.join();
        for (String[] s : new String[][] {{stage, state()}, {stage + "/new_thread", other[0]}}) {
            System.out.println(s[0] + " " + s[1]);
            if (!CLEAN.equals(s[1])) {
                failures.add(s[0]);
            }
        }
    }

    private static void compute(IntelPairHmm hmm) {
        byte[] hap = "ACGTTGCAAGGCTTACGATCGATCGGATCCTAGG".getBytes();
        ReadDataHolder read = new ReadDataHolder();
        read.readBases = "ACGTTGCAAGGCTTACGTTCGATCG".getBytes();
        read.readQuals = new byte[read.readBases.length];
        read.insertionGOP = new byte[read.readBases.length];
        read.deletionGOP = new byte[read.readBases.length];
        read.overallGCP = new byte[read.readBases.length];
        Arrays.fill(read.readQuals, (byte) 30);
        Arrays.fill(read.insertionGOP, (byte) 45);
        Arrays.fill(read.deletionGOP, (byte) 45);
        Arrays.fill(read.overallGCP, (byte) 10);
        HaplotypeDataHolder[] haps = new HaplotypeDataHolder[64];
        for (int i = 0; i < haps.length; i++) {
            haps[i] = new HaplotypeDataHolder();
            haps[i].haplotypeBases = Arrays.copyOfRange(hap, i % 8, hap.length);
        }
        double[] result = new double[haps.length];
        hmm.computeLikelihoods(new ReadDataHolder[] {read}, haps, result);
        for (double r : result) {
            if (!(r < 0 && r > -1e4)) {
                throw new IllegalStateException("Implausible likelihood " + r);
            }
        }
    }

    public static void main(String[] argv) throws Exception {
        utils = new IntelGKLUtils();
        if (!utils.load(null)) {
            throw new IllegalStateException("GKL utils unavailable");
        }
        check("jvm_start");
        int threads = Integer.parseInt(argv[0]);
        String library = threads > 1 ? "omp" : "single";
        IntelPairHmm hmm = threads > 1 ? new IntelPairHmmOMP() : new IntelPairHmm();
        if (!hmm.load(null)) {
            throw new IllegalStateException("Native PairHMM unavailable");
        }
        check(library + "/after_load");
        for (boolean useDouble : new boolean[] {false, true}) {
            String tag = library + (useDouble ? "_double" : "_float");
            PairHMMNativeArguments args = new PairHMMNativeArguments();
            args.maxNumberOfThreads = threads;
            args.useDoublePrecision = useDouble;
            hmm.initialize(args);
            check(tag + "/after_initialize");
            compute(hmm);
            check(tag + "/after_compute");
            hmm.done();
            check(tag + "/after_done");
        }
        boolean enforce = "aarch64".equals(System.getProperty("os.arch"));
        if (failures.isEmpty()) {
            System.out.println("FP-STATE-CLEAN: no flush-to-zero leak into Java threads");
        } else if (enforce) {
            System.out.println("FP-STATE-LEAK: " + failures);
            System.exit(1);
        } else {
            System.out.println("FP-STATE-LEAK (informational on " + System.getProperty("os.arch") + "): "
                    + failures);
        }
    }
}
