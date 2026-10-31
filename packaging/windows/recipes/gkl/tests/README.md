# Native kernel qualification

`GklParityProbe.java` exercises native float/double likelihoods, underflow/long
reads, repeatability, OMP1/2/4, exact SW CIGAR/offset over all overhang modes,
ties/gaps and invalid inputs. `omp_fp_state.cc` tests per-worker FTZ/DAZ and
restoration of the complete prior MXCSR, including other rounding-state bits.

`linux-avx2-golden.tsv` contains 40 likelihood and 28 SW outputs from the pinned
stock Linux GATK4.6.2.0/GKL0.8.11 libraries, JDK17.0.18, AVX=true/AVX2=true/
AVX512=false. Source GKL tag: `84c4aa4d72322e60a4d18bc8c59b455b1d5327f6`.
Inputs are deterministic synthetic sequences, not specimen data.

SHA256 provenance:

- GATK jar: `40b494b1e356931c9143be09c57ba6dfbb8d150167361aac2ceec24089b75667`
- Native utils: `349ea6409aca99faf7a65fdb2e7a0ed560c70cbd4757b3fd4e0e8b7a0c3f8f84`
- Native OMP PairHMM: `a8a1ebfb470c297a248d09f656c349f28971ceea0add316d176c436b8b315553`
- Native SW: `1b2a0226b1fa42a3df46eaebd59946e6296b037720825d92d5e56687b07481ef`

The comparator records every non-bit-exact likelihood and its absolute error;
1e-5 is the declared upstream native-probe STOP bound. SW must be exact.
This does not replace the strict real-chr20 site/genotype/numeric/FILTER gate.
