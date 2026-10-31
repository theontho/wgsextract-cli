import com.intel.gkl.IntelGKLUtils;
import com.intel.gkl.pairhmm.IntelPairHmm;
import com.intel.gkl.pairhmm.IntelPairHmmOMP;
import com.intel.gkl.smithwaterman.IntelSmithWaterman;
import java.io.File;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Random;
import org.broadinstitute.gatk.nativebindings.pairhmm.*;
import org.broadinstitute.gatk.nativebindings.smithwaterman.*;

public final class GklParityProbe {
    private static byte[] sequence(int length, long seed) {
        Random rng = new Random(seed);
        byte[] bases = new byte[length];
        byte[] alphabet = {'A', 'C', 'G', 'T'};
        for (int i = 0; i < length; i++) {
            bases[i] = alphabet[rng.nextInt(4)];
        }
        return bases;
    }

    private static byte[] quality(int length, int value) {
        byte[] result = new byte[length];
        Arrays.fill(result, (byte) value);
        return result;
    }

    private static void expect(Class<? extends RuntimeException> type, Runnable action) {
        try {
            action.run();
        } catch (RuntimeException e) {
            if (type.isInstance(e)) {
                return;
            }
            throw e;
        }
        throw new IllegalStateException("Expected " + type.getSimpleName());
    }

    public static void main(String[] argv) throws Exception {
        File temp = new File(argv[1]);
        IntelGKLUtils utils = new IntelGKLUtils();
        if (!utils.load(temp) || !utils.isAvx2Supported() || utils.isAvx512Supported()) {
            throw new IllegalStateException("Expected native common AVX/AVX2 ISA");
        }
        System.err.printf("Native AVX/AVX2, AVX512=false, OMP_AVAILABLE=%d%n",
                utils.getAvailableOmpThreads());
        ReadDataHolder[] reads = new ReadDataHolder[5];
        int[] lengths = {40, 76, 151, 512, 1280};
        int[] qualities = {2, 10, 30, 60, 60};
        for (int i = 0; i < reads.length; i++) {
            ReadDataHolder read = new ReadDataHolder();
            read.readBases = sequence(lengths[i], i + 17);
            read.readQuals = quality(lengths[i], qualities[i]);
            read.insertionGOP = quality(lengths[i], 40);
            read.deletionGOP = quality(lengths[i], 40);
            read.overallGCP = quality(lengths[i], 10);
            reads[i] = read;
        }
        HaplotypeDataHolder[] haps = new HaplotypeDataHolder[4];
        for (int i = 0; i < haps.length; i++) {
            haps[i] = new HaplotypeDataHolder();
            haps[i].haplotypeBases = sequence(i == 3 ? 1500 : 200, i + 17);
        }
        try (PrintWriter out = new PrintWriter(argv[0], "UTF-8")) {
            out.println("kind\tmode\tthreads\tcase\tvalue\tbits");
            for (boolean useDouble : new boolean[] {false, true}) {
                double[] expected = null;
                for (int threads : new int[] {0, 1, 2, 4}) {
                    IntelPairHmm pair = threads == 0 ? new IntelPairHmm() : new IntelPairHmmOMP();
                    if (!pair.load(temp)) {
                        throw new IllegalStateException("Native PairHMM unavailable");
                    }
                    PairHMMNativeArguments args = new PairHMMNativeArguments();
                    args.maxNumberOfThreads = Math.max(1, threads);
                    args.useDoublePrecision = useDouble;
                    pair.initialize(args);
                    expect(NullPointerException.class,
                            () -> pair.computeLikelihoods(null, haps, new double[0]));
                    double[] repeatExpected = null;
                    for (int repeat = 0; repeat < 3; repeat++) {
                        double[] result = new double[reads.length * haps.length];
                        pair.computeLikelihoods(reads, haps, result);
                        for (int i = 0; i < result.length; i++) {
                            if (Double.isNaN(result[i])) {
                                throw new IllegalStateException("NaN likelihood");
                            }
                            if (repeatExpected != null && Double.doubleToRawLongBits(result[i])
                                    != Double.doubleToRawLongBits(repeatExpected[i])) {
                                throw new IllegalStateException("Non-repeatable likelihood");
                            }
                            if (threads > 1 && Double.doubleToRawLongBits(result[i])
                                    != Double.doubleToRawLongBits(expected[i])) {
                                throw new IllegalStateException("OMP thread-count drift");
                            }
                            out.printf("pair\t%s\t%d\t%d:%d\t%s\t%s%n",
                                    useDouble ? "double" : "float", threads, repeat, i,
                                    Double.toHexString(result[i]),
                                    Long.toHexString(Double.doubleToRawLongBits(result[i])));
                        }
                        repeatExpected = result;
                    }
                    if (threads == 1) {
                        expected = repeatExpected;
                    }
                    pair.done();
                }
            }
            IntelSmithWaterman sw = new IntelSmithWaterman();
            if (!sw.load(temp)) {
                throw new IllegalStateException("Native Smith-Waterman unavailable");
            }
            SWParameters params = new SWParameters(200, -150, -260, -11);
            String[][] cases = {
                {"ACGTACGT", "ACGTACGT"},
                {"ACGTACGT", "ACGTTACGT"},
                {"ACGTACGT", "ACGACGT"},
                {"AAAACCCCGGGG", "AACCCCGG"},
                {"ATATATATAT", "ATATAT"},
                {"ACGTACGT", "TTACGTACGTTT"},
                {"ACGTACGT", "ACGAACGT"},
            };
            for (SWOverhangStrategy strategy : SWOverhangStrategy.values()) {
                for (int i = 0; i < cases.length; i++) {
                    byte[] ref = cases[i][0].getBytes(StandardCharsets.US_ASCII);
                    byte[] alt = cases[i][1].getBytes(StandardCharsets.US_ASCII);
                    SWNativeAlignerResult result = sw.align(ref, alt, params, strategy);
                    out.printf("sw\t%s\t0\t%d\t%s\t%d%n",
                            strategy, i, result.cigar, result.alignment_offset);
                }
            }
            expect(IllegalArgumentException.class,
                    () -> sw.align(new byte[0], new byte[] {'A'}, params, SWOverhangStrategy.INDEL));
            expect(IllegalArgumentException.class,
                    () -> sw.align(new byte[32768], new byte[] {'A'}, params, SWOverhangStrategy.INDEL));
            sw.close();
        }
        System.out.println("Native likelihood repeat/thread parity and SW probes passed");
    }
}
