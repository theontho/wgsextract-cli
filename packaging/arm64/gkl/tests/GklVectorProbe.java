package org.broadinstitute.hellbender.utils.pairhmm;

import com.intel.gkl.IntelGKLUtils;
import com.intel.gkl.pairhmm.IntelPairHmm;
import com.intel.gkl.pairhmm.IntelPairHmmOMP;
import com.intel.gkl.smithwaterman.IntelSmithWaterman;
import java.io.BufferedReader;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Random;
import org.broadinstitute.gatk.nativebindings.pairhmm.HaplotypeDataHolder;
import org.broadinstitute.gatk.nativebindings.pairhmm.PairHMMNativeArguments;
import org.broadinstitute.gatk.nativebindings.pairhmm.ReadDataHolder;
import org.broadinstitute.gatk.nativebindings.smithwaterman.SWOverhangStrategy;
import org.broadinstitute.gatk.nativebindings.smithwaterman.SWParameters;
import org.broadinstitute.hellbender.utils.smithwaterman.SmithWatermanJavaAligner;

/**
 * Architecture-neutral GKL kernel probe. Emits raw likelihood bits for native float (GATK's
 * default float-then-double path), native double and GATK's Java LoglessPairHMM, plus native
 * and Java Smith-Waterman CIGARs, so x86 AVX and Arm NEON outputs can be diffed bit-for-bit.
 *
 * Usage: GklVectorProbe GKL_TESTDATA_DIR OUT_DIR RANDOM_CASES THREADS
 */
public final class GklVectorProbe {
    private static final byte[] BASES = {'A', 'C', 'G', 'T'};
    private static final int BATCH_READS = 8;
    private static final int BATCH_HAPS = 4;

    private record Case(String source, byte[] hap, byte[] read, byte[] qual, byte[] ins,
                        byte[] del, byte[] gcp, double expected) {}

    private static byte[] normalize(byte[] scores, int min) {
        byte[] result = new byte[scores.length];
        for (int i = 0; i < scores.length; i++) {
            result[i] = (byte) Math.max(min, scores[i] - 33);
        }
        return result;
    }

    private static List<Case> testData(Path file) throws Exception {
        List<Case> cases = new ArrayList<>();
        for (String line : Files.readAllLines(file, StandardCharsets.UTF_8)) {
            if (line.isBlank() || line.startsWith("#")) {
                continue;
            }
            String[] f = line.trim().split("\\s+");
            cases.add(new Case("gkl-testdata", f[0].getBytes(StandardCharsets.US_ASCII),
                    f[1].getBytes(StandardCharsets.US_ASCII),
                    normalize(f[2].getBytes(StandardCharsets.US_ASCII), 6),
                    normalize(f[3].getBytes(StandardCharsets.US_ASCII), 0),
                    normalize(f[4].getBytes(StandardCharsets.US_ASCII), 0),
                    normalize(f[5].getBytes(StandardCharsets.US_ASCII), 0),
                    Double.parseDouble(f[6])));
        }
        return cases;
    }

    private static byte[] mutate(byte[] source, Random rng, double rate) {
        ByteArrayBuilder out = new ByteArrayBuilder(source.length + 16);
        for (byte b : source) {
            double r = rng.nextDouble();
            if (r < rate / 3) {
                out.add(BASES[rng.nextInt(4)]);
            } else if (r < 2 * rate / 3) {
                out.add(b);
                for (int k = rng.nextInt(4); k >= 0; k--) {
                    out.add(BASES[rng.nextInt(4)]);
                }
            } else if (r >= rate) {
                out.add(b);
            }
        }
        if (out.size() == 0) {
            out.add(BASES[rng.nextInt(4)]);
        }
        return out.toArray();
    }

    /** Haplotype/read sets in GATK's shape: one window, several haplotypes, many reads. */
    private static List<List<Case>> randomBatches(int count, long seed) {
        Random rng = new Random(seed);
        List<List<Case>> batches = new ArrayList<>();
        int made = 0;
        while (made < count) {
            int hapLength = 50 + rng.nextInt(550);
            byte[] window = new byte[hapLength];
            for (int i = 0; i < hapLength; i++) {
                window[i] = BASES[rng.nextInt(4)];
            }
            byte[][] haps = new byte[BATCH_HAPS][];
            haps[0] = window;
            for (int h = 1; h < BATCH_HAPS; h++) {
                haps[h] = mutate(window, rng, 0.005 + rng.nextDouble() * 0.05);
            }
            List<Case> batch = new ArrayList<>();
            for (int r = 0; r < BATCH_READS; r++) {
                byte[] source = haps[rng.nextInt(BATCH_HAPS)];
                int length = Math.min(source.length, 1 + rng.nextInt(rng.nextBoolean() ? 151 : 300));
                int start = rng.nextInt(source.length - length + 1);
                byte[] read = mutate(Arrays.copyOfRange(source, start, start + length), rng,
                        rng.nextDouble() * 0.3);
                if (rng.nextInt(20) == 0) {
                    read[rng.nextInt(read.length)] = 'N';
                }
                byte[] qual = new byte[read.length];
                byte[] ins = new byte[read.length];
                byte[] del = new byte[read.length];
                byte[] gcp = new byte[read.length];
                boolean lowQualityRead = rng.nextInt(10) == 0;
                for (int i = 0; i < read.length; i++) {
                    qual[i] = (byte) (lowQualityRead ? 2 + rng.nextInt(10) : 6 + rng.nextInt(55));
                    ins[i] = (byte) (rng.nextInt(4) == 0 ? 10 + rng.nextInt(50) : 45);
                    del[i] = (byte) (rng.nextInt(4) == 0 ? 10 + rng.nextInt(50) : 45);
                    gcp[i] = 10;
                }
                for (byte[] hap : haps) {
                    batch.add(new Case("random", hap, read, qual, ins, del, gcp, Double.NaN));
                    made++;
                }
            }
            batches.add(batch);
        }
        return batches;
    }

    /** Runs one GATK-shaped call: every read against every haplotype, read-major. */
    private static double[] runNative(IntelPairHmm hmm, List<Case> batch) {
        List<ReadDataHolder> reads = new ArrayList<>();
        List<HaplotypeDataHolder> haps = new ArrayList<>();
        int perRead = 0;
        for (Case c : batch) {
            if (reads.isEmpty() || reads.get(reads.size() - 1).readBases != c.read()) {
                ReadDataHolder read = new ReadDataHolder();
                read.readBases = c.read();
                read.readQuals = c.qual();
                read.insertionGOP = c.ins();
                read.deletionGOP = c.del();
                read.overallGCP = c.gcp();
                reads.add(read);
            }
            if (reads.size() == 1) {
                HaplotypeDataHolder hap = new HaplotypeDataHolder();
                hap.haplotypeBases = c.hap();
                haps.add(hap);
                perRead++;
            }
        }
        if (reads.size() * perRead != batch.size()) {
            throw new IllegalStateException("Batch is not a full read x haplotype matrix");
        }
        double[] result = new double[batch.size()];
        hmm.computeLikelihoods(reads.toArray(new ReadDataHolder[0]),
                haps.toArray(new HaplotypeDataHolder[0]), result);
        return result;
    }

    private static IntelPairHmm nativeHmm(boolean useDouble, int threads) {
        IntelPairHmm hmm = threads > 1 ? new IntelPairHmmOMP() : new IntelPairHmm();
        if (!hmm.load(null)) {
            throw new IllegalStateException("Native PairHMM unavailable");
        }
        PairHMMNativeArguments args = new PairHMMNativeArguments();
        args.maxNumberOfThreads = threads;
        args.useDoublePrecision = useDouble;
        hmm.initialize(args);
        return hmm;
    }

    private static String bits(double value) {
        return Long.toHexString(Double.doubleToRawLongBits(value));
    }

    // Stock GKL has no isNeonSupported(); the probe must also run against the unmodified x64 jar.
    private static boolean neonSupported(IntelGKLUtils utils) {
        try {
            return (Boolean) IntelGKLUtils.class.getMethod("isNeonSupported").invoke(utils);
        } catch (ReflectiveOperationException e) {
            return false;
        }
    }

    public static void main(String[] argv) throws Exception {
        Path data = Path.of(argv[0]);
        Path out = Path.of(argv[1]);
        int randomCases = Integer.parseInt(argv[2]);
        int threads = Integer.parseInt(argv[3]);
        Files.createDirectories(out);
        IntelGKLUtils utils = new IntelGKLUtils();
        if (!utils.load(null)) {
            throw new IllegalStateException("GKL utils unavailable");
        }
        System.err.printf("arch=%s avx=%s avx2=%s avx512=%s neon=%s omp=%d%n",
                System.getProperty("os.arch"), utils.isAvxSupported(), utils.isAvx2Supported(),
                utils.isAvx512Supported(), neonSupported(utils), utils.getAvailableOmpThreads());

        List<List<Case>> batches = new ArrayList<>();
        for (Case c : testData(data.resolve("pairhmm-testdata.txt"))) {
            batches.add(List.of(c));
        }
        batches.addAll(randomBatches(randomCases, 236L));

        // GKL keeps the precision mode in a process-global, so run the passes sequentially.
        IntelPairHmm hmm = nativeHmm(true, threads);
        List<double[]> doubles = new ArrayList<>();
        for (List<Case> batch : batches) {
            doubles.add(runNative(hmm, batch));
        }
        hmm.done();
        hmm = nativeHmm(false, threads);
        List<double[]> floats = new ArrayList<>();
        for (List<Case> batch : batches) {
            floats.add(runNative(hmm, batch));
        }
        LoglessPairHMM java = new LoglessPairHMM();
        java.initialize(1024, 1024);
        long cells = 0;
        try (PrintWriter tsv = new PrintWriter(out.resolve("pairhmm.tsv").toFile(), "UTF-8")) {
            tsv.println("source\tindex\tread_len\thap_len\tnative_float\tnative_double\tjava\texpected");
            int index = 0;
            for (int b = 0; b < batches.size(); b++) {
                List<Case> batch = batches.get(b);
                for (int i = 0; i < batch.size(); i++) {
                    Case c = batch.get(i);
                    double j = java.computeReadLikelihoodGivenHaplotypeLog10(c.hap(), c.read(),
                            c.qual(), c.ins(), c.del(), c.gcp(), true, null);
                    cells += (long) c.read().length * c.hap().length;
                    tsv.printf("%s\t%d\t%d\t%d\t%s\t%s\t%s\t%s%n", c.source(), index++,
                            c.read().length, c.hap().length, bits(floats.get(b)[i]),
                            bits(doubles.get(b)[i]), bits(j),
                            Double.isNaN(c.expected()) ? "" : bits(c.expected()));
                }
            }
        }

        // Kernel throughput: repeat the float pass and time only the native calls.
        long best = Long.MAX_VALUE;
        for (int repeat = 0; repeat < 5; repeat++) {
            long start = System.nanoTime();
            for (List<Case> batch : batches) {
                runNative(hmm, batch);
            }
            best = Math.min(best, System.nanoTime() - start);
        }
        hmm.done();

        IntelSmithWaterman sw = new IntelSmithWaterman();
        if (!sw.load(null)) {
            throw new IllegalStateException("Native Smith-Waterman unavailable");
        }
        SWParameters params = new SWParameters(200, -150, -260, -11);
        List<byte[]> lines = new ArrayList<>();
        try (BufferedReader in = Files.newBufferedReader(data.resolve("smith-waterman.SOFTCLIP.in"))) {
            for (String line = in.readLine(); line != null; line = in.readLine()) {
                lines.add(line.getBytes(StandardCharsets.US_ASCII));
            }
        }
        SmithWatermanJavaAligner javaSw = SmithWatermanJavaAligner.getInstance();
        int javaDifferences = 0;
        try (PrintWriter tsv = new PrintWriter(out.resolve("sw.tsv").toFile(), "UTF-8")) {
            tsv.println("strategy\tindex\tnative_cigar\tnative_offset\tjava_cigar\tjava_offset");
            for (SWOverhangStrategy strategy : SWOverhangStrategy.values()) {
                for (int i = 0; i + 1 < lines.size(); i += 2) {
                    var n = sw.align(lines.get(i), lines.get(i + 1), params, strategy);
                    var j = javaSw.align(lines.get(i), lines.get(i + 1), params, strategy);
                    String javaCigar = j.getCigar().toString();
                    if (!javaCigar.equals(n.cigar) || j.getAlignmentOffset() != n.alignment_offset) {
                        javaDifferences++;
                    }
                    tsv.printf("%s\t%d\t%s\t%d\t%s\t%d%n", strategy, i / 2, n.cigar,
                            n.alignment_offset, javaCigar, j.getAlignmentOffset());
                }
            }
        }
        long swBest = Long.MAX_VALUE;
        for (int repeat = 0; repeat < 3; repeat++) {
            long start = System.nanoTime();
            for (int i = 0; i + 1 < lines.size(); i += 2) {
                sw.align(lines.get(i), lines.get(i + 1), params, SWOverhangStrategy.SOFTCLIP);
            }
            swBest = Math.min(swBest, System.nanoTime() - start);
        }
        sw.close();
        try (PrintWriter json = new PrintWriter(out.resolve("timing.json").toFile(), "UTF-8")) {
            json.printf("{\"arch\": \"%s\", \"threads\": %d, \"pairhmm_cases\": %d, "
                    + "\"pairhmm_cells\": %d, \"pairhmm_float_best_seconds\": %.6f, "
                    + "\"pairhmm_gcups\": %.4f, \"sw_pairs\": %d, \"sw_softclip_best_seconds\": %.6f, "
                    + "\"sw_native_vs_java_differences\": %d}%n",
                    System.getProperty("os.arch"), threads,
                    batches.stream().mapToInt(List::size).sum(), cells, best * 1e-9,
                    cells / (best * 1e-9) / 1e9, lines.size() / 2, swBest * 1e-9, javaDifferences);
        }
        System.err.println("GklVectorProbe complete: " + out);
    }

    private static final class ByteArrayBuilder {
        private byte[] data;
        private int size;

        ByteArrayBuilder(int capacity) {
            data = new byte[capacity];
        }

        void add(byte value) {
            if (size == data.length) {
                data = Arrays.copyOf(data, data.length * 2 + 1);
            }
            data[size++] = value;
        }

        int size() {
            return size;
        }

        byte[] toArray() {
            return Arrays.copyOf(data, size);
        }
    }
}
