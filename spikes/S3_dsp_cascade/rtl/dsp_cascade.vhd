-- S3 spike: does behavioral inference create PCOUT->PCIN cascades?
-- A 4-tap signed MAC chain with mixed add/subtract (the shape the FFT
-- Karatsuba combine needs: im = m3 - m1 - m2).
library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;

entity dsp_cascade is
port (
    clk : in  std_logic;
    a0, b0 : in  signed(17 downto 0);
    a1, b1 : in  signed(17 downto 0);
    a2, b2 : in  signed(17 downto 0);
    a3, b3 : in  signed(17 downto 0);
    y      : out signed(41 downto 0)
);
end entity;

architecture rtl of dsp_cascade is
    signal p0, p1, p2 : signed(35 downto 0);
    signal p3         : signed(41 downto 0);
begin
    process(clk)
    begin
        if rising_edge(clk) then
            p0 <= a0 * b0;              -- tap 0: bare product (PREG)
            p1 <= p0 + (a1 * b1);       -- tap 1: PCIN + product (add)
            p2 <= p1 - (a2 * b2);       -- tap 2: PCIN - product (sub)
            p3 <= resize(p2, 42)
                  - resize(signed'(a3 * b3), 42);  -- tap 3: sub again
        end if;
    end process;
    y <= p3;
end architecture;
