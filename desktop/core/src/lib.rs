/// Build a request frame:  AB 00 LEN FF CMD SUB [payload]
/// LEN counts the bytes from FF onward (not from the start).
pub fn frame(cmd: u8, sub: u8, payload: &[u8]) -> Vec<u8> {
    let mut body = vec![0xFF, cmd, sub];
    body.extend_from_slice(payload);

    let mut out = vec![0xAB, 0x00, body.len() as u8];
    out.extend(body);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vibrate_packet() {
        // The "find band" command from the README.
        assert_eq!(frame(0x71, 0x80, &[]), [0xAB, 0x00, 0x03, 0xFF, 0x71, 0x80]);
    }

    #[test]
    fn client_hello_packet() {
        // AB 00 06 FF 20 80 01 00 00
        assert_eq!(
            frame(0x20, 0x80, &[0x01, 0x00, 0x00]),
            [0xAB, 0x00, 0x06, 0xFF, 0x20, 0x80, 0x01, 0x00, 0x00]
        );
    }
}