/*
 * ALGOps example — a debounced threshold detector in C (contract v2, no SDK needed).
 *
 * Inputs (set by the runner):
 *   ALGOPS_PARAM_FILTER_THRESHOLD   keep a frame when score >= threshold ...
 *   ALGOPS_PARAM_FILTER_MIN_STREAK  ... for at least this many consecutive frames (debounce)
 *   ALGOPS_SUITE_PATH               directory with frames.txt  ("score label" per line)
 *   ALGOPS_RESULT                   where to write result.json
 * Output: {"status":"ok","metrics":{...},"counts":{"tp":..,"fp":..,"fn":..,"tn":..}}
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static const char *env_or_die(const char *name) {
    const char *v = getenv(name);
    if (!v || !*v) {
        fprintf(stderr, "missing environment variable %s\n", name);
        exit(2);
    }
    return v;
}

int main(void) {
    double threshold = atof(env_or_die("ALGOPS_PARAM_FILTER_THRESHOLD"));
    int min_streak = atoi(env_or_die("ALGOPS_PARAM_FILTER_MIN_STREAK"));
    char path[4096];
    snprintf(path, sizeof path, "%s/frames.txt", env_or_die("ALGOPS_SUITE_PATH"));

    FILE *in = fopen(path, "r");
    if (!in) { perror(path); return 3; }
    static double score[200000];
    static int label[200000];
    int n = 0;
    while (n < 200000 && fscanf(in, "%lf %d", &score[n], &label[n]) == 2) n++;
    fclose(in);

    clock_t t0 = clock();
    long tp = 0, fp = 0, fn = 0, tn = 0;
    int streak = 0;
    for (int i = 0; i < n; i++) {
        streak = (score[i] >= threshold) ? streak + 1 : 0;
        int kept = streak >= min_streak;
        if (kept && label[i]) tp++;
        else if (kept) fp++;
        else if (label[i]) fn++;
        else tn++;
    }
    double us_per_frame = 1e6 * (double)(clock() - t0) / CLOCKS_PER_SEC / (n ? n : 1);

    FILE *out = fopen(env_or_die("ALGOPS_RESULT"), "w");
    if (!out) { perror("result"); return 4; }
    fprintf(out,
            "{\"status\":\"ok\",\"n_items\":%d,"
            "\"metrics\":{\"latency_us\":%.6f},"
            "\"counts\":{\"tp\":%ld,\"fp\":%ld,\"fn\":%ld,\"tn\":%ld}}\n",
            n, us_per_frame, tp, fp, fn, tn);
    fclose(out);
    return 0;
}
