export class OrderService {
  find(id: string): string {
    return normalise(id)
  }
}

export function normalise(value: string): string {
  return value.trim()
}
