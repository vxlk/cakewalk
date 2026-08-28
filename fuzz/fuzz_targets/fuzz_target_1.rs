#![no_main]
use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    // This is a stub fuzzer. 
    // To truly fuzz fastfs, you would parse `data` into a tree of files in a TempDir, 
    // then run `fastfs::_fastfs::live_walk` on that directory to ensure it doesn't panic
    // on weird Unicode sequences, cyclic symlinks, or extreme file depths.
});
