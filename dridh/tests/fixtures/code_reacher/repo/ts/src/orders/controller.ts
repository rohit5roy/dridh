import { Controller, Get, Post } from '@nestjs/common'
import { OrderService } from './service'

@Controller('orders')
export class OrdersController {
  constructor(private readonly service: OrderService) {}

  @Get(':id')
  find(id: string): string {
    return this.service.find(id)
  }

  @Post()
  create(body: object): object {
    return body
  }
}
